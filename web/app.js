// SPDX-License-Identifier: BUSL-1.1
"use strict";

/* ProcWorks - schlanker Web-Client (Roadmap-Schritt 13, Abschnitt 8).
 *
 * Die GUI ist ein reiner Client der headless FastAPI: sie sammelt Intentionen
 * und rendert Zustand. Jede Korrektheitsentscheidung (K/D/Z/A/C/H/F/R/M)
 * trifft ausschliesslich der Kern - hier liegt keine Validierungslogik.
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

// --------------------------------------------------------------------------
// Meldungskatalog fuer Regelbefunde
// --------------------------------------------------------------------------

/**
 * Deutsche Texte je Befund-Code (``ValidationFinding.code``).
 *
 * Der Kern bleibt sprachneutral: Er liefert neben der technischen, englischen
 * ``message`` einen Code und die menschenlesbaren Teile (Schritt- und
 * Elementnamen, nie IDs) als ``params``. Formuliert wird ausschliesslich hier --
 * EIN Katalog fuer alle Anzeigestellen (Fehlermeldung, Befundliste, Karte,
 * Statusleiste, Marker am Knoten, Migrationsassistent), damit sie nicht
 * auseinanderlaufen. Frueher erschienen Ablehnungen englisch und mit interner
 * Knoten-ID („act_246“).
 *
 * Jeder Eintrag liefert ``{ text, hint }``: ``text`` sagt, was nicht geht,
 * ``hint`` (optional) was zu tun ist. Fehlt ein Code, faellt ``findingText``
 * auf die Kernmeldung zurueck; ``FINDING_FALLBACK_RULES`` im Waechter
 * dokumentiert, welche Regeln bewusst noch nicht uebersetzt sind.
 */
/** Namen der Objektarten fuer „gibt es nicht / gibt es schon“ (Code OP.*). */
const OP_KIND_NAMES = {
  node: "Der Schritt", data_element: "Das Datenelement", role: "Die Rolle", org_unit: "Die Abteilung",
  agent: "Die Person", follow_up: "Der Folgeprozess", activity_template: "Die Dienst-Vorlage", connector: "Der Connector",
  schema: "Der Prozess", template: "Die Vorlage", org_model: "Die Organisation",
};

/** Texte fuer „geht an dieser Knotenart nicht“ (Code OP.wrong-node-kind, Parameter ``what``). */
const OP_WRONG_KIND = {
  loop_decision: "Eine Wiederholungsbedingung gehört an ein Schleifenende.",
  sync: "Warte-Beziehungen verbinden nur Aufgaben-Schritte.",
  rename: "Nur Schritte und Teilprozesse lassen sich umbenennen.",
  move: "Nur Schritte und Teilprozesse lassen sich verschieben.",
  empty_branch: "Einen leeren Zweig gibt es nur an einer Entscheidung.",
  data_access: "Daten lassen sich nur an Aufgaben-Schritte binden.",
  input_mask: "Eine Eingabemaske gibt es nur an Aufgaben-Schritten.",
  service: "Ein Dienst lässt sich nur an Aufgaben-Schritte binden.",
  staff_rule: "Bearbeiter lassen sich nur Aufgaben-Schritten zuordnen.",
  subprocess_convert: "Nur ein Aufgaben-Schritt lässt sich in einen Teilprozess umwandeln.",
  subprocess: "Das ist kein Teilprozess-Schritt.",
  value_class: "Eine Wertklasse tragen nur Schritte und Teilprozesse.",
  automation: "Eine Automatik lässt sich nur an Aufgaben-Schritten einstellen.",
  priority: "Eine Priorität tragen nur Schritte und Teilprozesse.",
  mail: "Eine E-Mail-Benachrichtigung gibt es nur an Aufgaben-Schritten.",
  time_constraint: "Zeitvorgaben tragen nur Schritte und Teilprozesse.",
};

const FINDING_TEXTS = {
  "D1.read-before-write": (p) => ({
    text: `„${p.step}“ würde „${p.element}“ lesen, bevor es auf jedem Weg geschrieben wurde.`,
    hint: `Den Schritt hinter einen Schritt legen, der „${p.element}“ schreibt – oder die Bindung nicht als Pflicht setzen.`,
  }),
  "K2.start-count": (p) => ({ text: `Ein Prozess braucht genau einen Start, hier sind es ${p.count}.` }),
  "K2.end-count": (p) => ({ text: `Ein Prozess braucht genau ein Ende, hier sind es ${p.count}.` }),
  "K1.unbalanced": (p) => ({
    text: `Die Verzweigungen sind nicht vollständig: ${p.splits}× ${p.kind}, aber ${countLabel(Number(p.joins), "passende Zusammenführung", "passende Zusammenführungen")}.`,
    hint: "Jede Verzweigung braucht genau eine Zusammenführung derselben Art. Beim BPMN-Import zählt auch ein Schritt mit mehreren ausgehenden bzw. eingehenden Pfeilen als Verzweigung bzw. Zusammenführung.",
  }),
  "K1.wrong-join": (p) => ({
    text: `„${p.split}“ wird von „${p.join}“ geschlossen – das ist die falsche Art der Zusammenführung.`,
    hint: "Eine parallele Verzweigung schließt eine parallele Zusammenführung, eine Entscheidung eine Entscheidungs-Zusammenführung.",
  }),
  "K1.shared-join": (p) => ({
    text: `„${p.join}“ schließt zwei Verzweigungen zugleich – die Blöcke überkreuzen sich.`,
    hint: "Jeder Block muss vollständig innerhalb oder außerhalb eines anderen liegen.",
  }),
  "K1.unpaired": (p) => ({
    text: `Zu „${p.step}“ gibt es keine eindeutig passende Zusammenführung.`,
    hint: "Das Modell ist nicht sauber blockstrukturiert (typisch bei importiertem BPMN).",
  }),
  "K6.crosses-branch": (p) => ({
    text: `Die Schleife ab „${p.loop}“ überkreuzt die Verzweigung „${p.split}“: Anfang und Ende liegen nicht im selben Zweig.`,
    hint: "Eine Schleife muss ganz innerhalb eines Zweigs liegen oder die ganze Verzweigung umschließen (typisch bei importiertem BPMN).",
  }),
  "K3.unreachable": (p) => ({
    text: `„${p.step}“ ist vom Start aus nicht erreichbar.`,
    hint: "Typisch bei importiertem BPMN: der Schritt hängt an keiner Verbindung vom Start.",
  }),
  "K3.dead-end": (p) => ({
    text: `Von „${p.step}“ aus geht es nicht zum Ende weiter (Sackgasse).`,
    hint: "Jeder Schritt braucht einen Weg zum Ende.",
  }),
  "D2.parallel-writes": (p) => ({
    text: `„${p.a}“ und „${p.b}“ laufen parallel und schreiben beide „${p.element}“ – welcher Wert gilt, wäre Zufall.`,
    hint: "Nur einen der beiden Schritte schreiben lassen oder die Schritte nacheinander anordnen.",
  }),
  "Z2.nobody": (p) => ({
    text: `Für „${p.step}“ gibt es niemanden, der die Bearbeiterregel erfüllt.`,
    hint: "Der Rolle oder Abteilung mindestens eine Person zuordnen.",
  }),
  "Z3.reference-not-before": (p) => ({
    text: `„${p.step}“ bezieht sich auf die Person aus „${p.ref}“ – dieser Schritt läuft aber nicht auf jedem Weg vorher.`,
    hint: "Einen Schritt wählen, der garantiert vorher erledigt ist.",
  }),
  "Z4.automatic-with-staff": (p) => ({
    text: `„${p.step}“ läuft automatisch und darf deshalb keine Bearbeiterzuordnung haben.`,
  }),
  "T2.deadline": (p) => ({
    text: `Der längste Weg dauert ${fmtDuration(Number(p.critical))} und passt nicht in den Termin von ${fmtDuration(Number(p.deadline))}.`,
    hint: "Soll-Zeiten der Schritte verkürzen oder den Termin verlängern.",
  }),
  "B2.no-staff": (p) => ({
    text: `„${p.step}“ hat keine Bearbeiterzuordnung und würde in keiner Arbeitsliste erscheinen.`,
    hint: "Dem Schritt eine Rolle, Abteilung oder Person zuordnen.",
  }),
  "R0.not-draft": () => ({
    text: "Dieses Schema ist freigegeben und damit unveränderlich.",
    hint: "Für Änderungen eine neue Revision anlegen.",
  }),
  "LC.not-draft": () => ({ text: "Nur ein Entwurf kann freigegeben werden." }),
  "LC.not-released": () => ({ text: "Eine neue Revision lässt sich nur von einer freigegebenen Version anlegen." }),
  "D3.unknown-element": (p) => ({ text: `Das Datenelement „${p.element}“ gibt es in diesem Schema nicht.` }),
  "D6.not-writable": (p) => ({
    text: `„${p.step}“ setzt „${p.element}“ nicht – dieser Wert lässt sich beim Abschluss nicht ändern.`,
    hint: "Werte gibt nur der Schritt ein, der sie schreibt. Eine Korrektur nimmt die Prozessverantwortung vor.",
  }),
  "D6.unknown-element": (p) => ({ text: `„${p.element}“ ist kein Datenelement dieses Prozesses.` }),
  "D3.wrong-type": (p) => ({
    text: `Der Wert für „${p.element}“ passt nicht zum Typ ${typeName(p.type)}.`,
    hint: p.type === "DECIMAL" ? "Ein Betrag hat höchstens zwei Nachkommastellen." : undefined,
  }),
  "M0.not-candidate": () => ({ text: "Diese Instanz läuft nicht auf einer früheren Version dieses Schemas." }),
  "M1.not-released": (p) => ({
    text: `Die Zielversion v${p.version} ist noch nicht freigegeben.`,
    hint: "Erst freigeben, dann migrieren.",
  }),
  "M1.incorrect": (p) => ({ text: `Die Zielversion ist nicht korrekt (Regel ${p.rule}).` }),
  "M2.step-removed": (p) => ({
    text: `Der bereits bearbeitete Schritt „${p.step}“ fehlt in der neuen Version.`,
    hint: "Bereits erledigte Schritte dürfen in der neuen Version nicht entfernt werden. Diese Instanz läuft sicher auf ihrer Version weiter.",
  }),
  "M2.step-changed": (p) => ({ text: `Der bereits bearbeitete Schritt „${p.step}“ ist in der neuen Version kein Schritt derselben Art mehr.` }),
  "M2.path-changed": (p) => ({
    text: `Die neue Version ändert den bereits durchlaufenen Weg zwischen „${p.from}“ und „${p.to}“.`,
    hint: "Eine laufende Instanz kann nur in den Teil wechseln, der noch vor ihr liegt – der bereits durchlaufene Weg muss gleich bleiben.",
  }),
  "M3.rewired": (p) => ({
    text: `Nach dem erledigten Schritt „${p.step}“ ginge es in der neuen Version anders weiter.`,
    hint: "Die Instanz steht schon dahinter – sie läuft sicher auf ihrer Version weiter.",
  }),
  "M3.running-removed": (p) => ({ text: `Der gerade laufende Schritt „${p.step}“ ist in der neuen Version kein ausführbarer Schritt mehr.` }),
  "M4.missing-data": (p) => ({
    text: `„${p.step}“ braucht „${p.element}“, aber die Instanz hat dafür keinen Wert, und kein späterer Schritt liefert ihn.`,
    hint: `Einen Startwert für „${p.element}“ angeben.`,
  }),
  // --- Ziel-Pruefung der Webhooks (Regel I6, SSRF) -----------------------
  // Der Kern bleibt sprachneutral; formuliert wird hier, wie bei jedem Befund.
  // Laufzeit: Die Engine lehnt eine Aktion ab (409). Bis 1.28.0 kam
  // nur der englische Text, etwa „activity 'act_1' is already claimed by
  // 'a-tom' (W1)“. ``step`` ist der Schrittname, ``agent`` eine Agenten-ID.
  "EX.not-allowed": () => ({ text: "Das ist im aktuellen Zustand des Vorgangs nicht möglich." }),
  "EX.not-released": () => ({
    text: "Nur ein freigegebener Prozess lässt sich starten.",
    hint: "Den Entwurf freigeben oder als Prüfinstanz starten.",
  }),
  "EX.automatic": (p) => ({ text: `${stepOf(p)} ist ein automatischer Schritt – ihn übernimmt niemand von Hand.` }),
  "EX.not-ready": (p) => ({
    text: `${stepOf(p)} ist gerade nicht zu bearbeiten – der Vorgang steht an anderer Stelle.`,
    hint: "Die Ansicht aktualisieren; vermutlich hat jemand anderes den Schritt schon erledigt.",
  }),
  "EX.claimed-by-other": (p) => ({
    text: `${stepOf(p)} hat bereits ${p.agent ? agentNameOf(p.agent) : "jemand anderes"} übernommen.`,
    hint: "Abschließen oder zurücklegen kann nur, wer den Schritt übernommen hat.",
  }),
  "EX.not-eligible": (p) => ({ text: `Für ${stepOf(p)} bist du nicht zuständig.` }),
  "EX.failed": (p) => ({
    text: `${stepOf(p)} ist als gescheitert gemeldet und wartet auf den Wiederanlauf.`,
    hint: "Erst den Wiederanlauf auslösen, dann weiterarbeiten.",
  }),
  "EX.not-claimed": (p) => ({ text: `${stepOf(p)} hat niemand übernommen – es gibt nichts zurückzulegen.` }),
  "EX.suspended-by-other": (p) => ({ text: `${stepOf(p)} hat eine andere Person angehalten.` }),
  "EX.not-suspended": (p) => ({ text: `${stepOf(p)} ist nicht angehalten.` }),
  "EX.owner-only": (p) => ({ text: `Das darf bei ${stepOf(p)} nur, wer den Schritt übernommen hat.` }),
  "EX.suspended": (p) => ({ text: `${stepOf(p)} ist angehalten – bitte zuerst fortsetzen.` }),
  "EX.not-failed": (p) => ({ text: `${stepOf(p)} ist nicht als gescheitert gemeldet.` }),
  "EX.loop-broken": () => ({
    text: "Eine Schleife wiederholt sich ohne Arbeit dazwischen – das Modell ist nicht sauber geschachtelt.",
    hint: "Das Schema in einer neuen Revision korrigieren und den Vorgang migrieren.",
  }),
  "EX.no-decision": () => ({ text: "Eine Entscheidung hat keine Regel, nach der sie verzweigt." }),
  "EX.value-missing": (p) => ({
    text: `Für die Entscheidung fehlt der Wert „${p.element}“.`,
    hint: "Den Wert im Schritt davor setzen.",
  }),
  "EX.no-branch": (p) => ({ text: `Für den Wert von „${p.element}“ passt kein Zweig der Entscheidung.` }),
  "EX.follow-up-condition": () => ({ text: "Die Bedingung eines Folgeprozesses lässt sich nicht auswerten." }),
  "EX.target-missing": () => ({ text: "Ein Teil- oder Folgeprozess ist nicht (mehr) vorhanden." }),
  "EX.not-running": () => ({ text: "Der Vorgang läuft nicht mehr." }),
  "EX.unknown-step": () => ({ text: "Diesen Schritt gibt es im Vorgang nicht." }),
  "EX.not-activity": (p) => ({ text: `${stepOf(p)} ist kein Aufgaben-Schritt.` }),
  // BPMN-Import: vorher roh „unsupported BPMN element 'inclusiveGateway'“.
  "BPMN.invalid": () => ({ text: "Die BPMN-Datei lässt sich nicht übernehmen." }),
  "BPMN.no-process": () => ({ text: "Die Datei enthält keinen Prozess (<process>)." }),
  "BPMN.bad-extension": () => ({
    text: "Die ProcWorks-Zusatzdaten in der Datei sind beschädigt.",
    hint: "Die Datei ohne den Block <extensionElements> importieren oder neu exportieren.",
  }),
  "BPMN.invalid-xml": () => ({
    text: "Die Datei ist kein gültiges XML.",
    hint: "Ist es wirklich die .bpmn-Datei aus dem Modellierwerkzeug?",
  }),
  "BPMN.flow-incomplete": () => ({ text: "Ein Pfeil (sequenceFlow) hat keinen Anfang oder kein Ziel." }),
  "BPMN.missing-id": (p) => ({ text: `Ein Element <${p.element}> hat keine ID.` }),
  "BPMN.unsupported": (p) => ({
    text: `ProcWorks übernimmt das Element „${bpmnElementName(p.element)}“ nicht.`,
    hint: "Unterstützt werden Start, Ende, Aufgaben, Teilprozesse sowie exklusive (XOR) und parallele (UND) Gateways in sauberer Blockstruktur.",
  }),
  "BPMN.mixed-gateway": () => ({
    text: "Ein Gateway verzweigt und führt zugleich zusammen.",
    hint: "In zwei Gateways aufteilen: eines führt zusammen, das nächste verzweigt.",
  }),
  "WH.egress-locked": () => ({
    text: "Auf dieser Instanz sind ausgehende Verbindungen gesperrt.",
    hint: "Der Probelauf zeigt trotzdem, was gesendet würde.",
  }),
  "WH.scheme": (p) => ({
    text: `Die Adresse beginnt mit „${p.scheme}“ – erlaubt sind nur http und https.`,
  }),
  "WH.no-host": () => ({ text: "Die Adresse nennt keinen Server." }),
  "WH.credentials": () => ({
    text: "Die Adresse darf keine Zugangsdaten enthalten.",
    hint: "Ein Secret wird serverseitig hinterlegt und als Referenz angegeben.",
  }),
  "WH.port": () => ({ text: "Die Adresse hat keinen gültigen Port." }),
  "WH.not-allow-listed": (p) => ({
    text: `„${p.host}“ steht nicht auf der Freigabeliste dieser Instanz.`,
    hint: "Die Liste pflegt die Administration (PROCWORKS_WEBHOOK_ALLOWLIST).",
  }),
  "WH.no-resolve": (p) => ({
    text: `Der Name „${p.host}“ lässt sich nicht auflösen.`,
    hint: "Tippfehler, oder der Name existiert nur im internen Netz.",
  }),
  "WH.internal-address": (p) => ({
    text: `„${p.host}“ zeigt auf eine interne Adresse – solche Ziele sind nicht zulässig.`,
    hint: "Das verhindert, dass ein Modell nach innen telefoniert (SSRF-Schutz).",
  }),

  // --- Benutzerverwaltung -------------------------------------------------
  "USERS.delete-self": () => ({
    text: "Den eigenen Login kann man nicht löschen – sonst wäre man ausgesperrt.",
    hint: "Von einem anderen Administrator-Login aus löschen.",
  }),
  "USERS.last-admin": (p) => ({
    text: `„${p.login}“ ist der letzte Login mit der Rolle Administrator und bleibt deshalb bestehen.`,
    hint: "Erst einen weiteren Administrator-Login anlegen.",
  }),
  "USERS.login-taken": (p) => ({
    text: `Den Login „${p.login}“ gibt es schon.`,
    hint: "Einen anderen Login-Namen w\u00E4hlen oder den vorhandenen Login in der Benutzerverwaltung zur\u00FCcksetzen.",
  }),
  "USERS.unknown-role": (p) => ({ text: `Diese Rolle gibt es nicht: ${p.roles}.` }),
  // Passwortregeln: je Grund ein eigener Satz statt einer Sammelmeldung.
  "PW.too-short": (p) => ({ text: `Das neue Passwort ist zu kurz \u2013 mindestens ${p.min} Zeichen.` }),
  "PW.unchanged": () => ({ text: "Das neue Passwort muss sich vom bisherigen unterscheiden." }),

  // --- Vorbedingungen der Operationen (Regel OP) -------------------------
  "OP.not-found": (p) => ({
    text: `${OP_KIND_NAMES[p.kind] || "Das Element"} „${nm(p.name)}“ gibt es nicht (mehr).`,
    hint: "Ansicht neu laden – vermutlich wurde es inzwischen geändert oder entfernt.",
  }),
  "OP.already-exists": (p) => ({
    text: `${OP_KIND_NAMES[p.kind] || "Ein Element"} mit der Kennung „${p.name}“ gibt es schon.`,
    hint: "Eine andere Kennung wählen oder das vorhandene Element verwenden.",
  }),
  "OP.no-edge": (p) => ({
    text: `Zwischen „${p.source}“ und „${p.target}“ gibt es keine Verbindung, auf der sich einfügen ließe.`,
    hint: "Ansicht neu laden – vermutlich wurde das Modell inzwischen geändert.",
  }),
  "OP.after-end": () => ({ text: "Hinter dem Ende lässt sich nichts einfügen.", hint: "Am „+“ vor dem Ende einfügen." }),
  "OP.anchor-not-serial": () => ({
    text: "Hier lässt sich nicht einfügen – der Schritt hat mehrere Nachfolger.",
    hint: "Das „+“ auf der Verbindungslinie verwenden, an der eingefügt werden soll.",
  }),
  "OP.too-few-branches": () => ({ text: "Eine Verzweigung braucht mindestens zwei Zweige." }),
  "OP.discriminator-type": (p) => ({
    text: p.where === "LOOP"
      ? "Dieses Datenelement kann nicht über eine Wiederholung entscheiden."
      : "Nach diesem Datenelement kann nicht verzweigt werden.",
    hint: "Ein Element vom Typ Ja/Nein, Zahl oder Text wählen.",
  }),
  "OP.loop-discriminator-source": () => ({
    text: "Über die Wiederholung kann nur ein Datenelement des Vorgangs entscheiden, kein extern geliefertes.",
  }),
  "OP.loop-discriminator-type": () => ({
    text: "Für diese Wiederholungsbedingung muss das Merkmal Ja/Nein sein.",
    hint: "Ein Ja/Nein-Element wählen oder die Wiederhol-Werte angeben.",
  }),
  "OP.label-missing": () => ({ text: "Der neue Schritt braucht eine Bezeichnung." }),
  "OP.sync-self": () => ({ text: "Ein Schritt kann nicht auf sich selbst warten." }),
  "OP.sync-exists": () => ({ text: "Diese Warte-Beziehung gibt es schon." }),
  "OP.sync-missing": () => ({ text: "Diese Warte-Beziehung gibt es nicht." }),
  "OP.sync-sets-empty": () => ({ text: "Für einen Querschritt bitte Schritte davor und danach wählen." }),
  "OP.sync-not-parallel": () => ({
    text: "Warte-Beziehungen gibt es nur zwischen Schritten paralleler Zweige desselben Blocks.",
  }),
  "OP.move-self": () => ({ text: "Ein Schritt kann nicht hinter sich selbst verschoben werden." }),
  "OP.not-serial": () => ({
    text: "Dieser Schritt steht an einer Verzweigung und lässt sich so nicht verschieben oder entfernen.",
    hint: "Den ganzen Block über seine Verzweigung bearbeiten.",
  }),
  "OP.sole-loop-node": () => ({
    text: "Das ist der einzige Schritt der Schleife.",
    hint: "Erst einen weiteren Schritt in die Schleife einfügen oder die Schleife als Ganzes entfernen.",
  }),
  "OP.sole-parallel-node": () => ({
    text: "Das ist der einzige Schritt dieses parallelen Zweigs.",
    hint: "Erst einen weiteren Schritt einfügen oder den Zweig entfernen.",
  }),
  "OP.last-xor-branch": () => ({
    text: "Eine Entscheidung braucht mindestens einen Zweig mit Schritten.",
    hint: "Die ganze Verzweigung über die Entscheidung entfernen.",
  }),
  "OP.delete-start-end": () => ({ text: "Start und Ende lassen sich nicht entfernen." }),
  "OP.delete-split-instead": () => ({
    text: "Eine Zusammenführung wird nicht einzeln entfernt.",
    hint: "Die Verzweigung auswählen und entfernen – dann geht der ganze Block auf einmal.",
  }),
  "OP.delete-loop-instead": () => ({
    text: "Ein Schleifenende wird nicht einzeln entfernt.",
    hint: "Den Schleifenanfang auswählen und entfernen – dann geht die ganze Schleife auf einmal.",
  }),
  "OP.block-unclear": () => ({
    text: "Zu dieser Verzweigung lässt sich der Block nicht eindeutig bestimmen.",
    hint: "Das Modell ist nicht sauber blockstrukturiert – bitte melden, wenn es so entstanden ist.",
  }),
  "OP.no-empty-branch": () => ({ text: "Diese Entscheidung hat keinen leeren Zweig." }),
  "OP.no-data-access": () => ({ text: "Diese Datenbindung gibt es an diesem Schritt nicht." }),
  "OP.mask-empty": () => ({ text: "Eine Eingabemaske braucht mindestens ein Feld." }),
  "OP.mask-duplicate-field": () => ({
    text: "Ein Datenelement steht zweimal in der Maske.",
    hint: "Jedes Element nur einmal aufnehmen.",
  }),
  "OP.mask-columns": () => ({ text: "Eine Maske hat eine bis drei Spalten." }),
  "OP.no-mask": () => ({ text: "Dieser Schritt hat keine Eingabemaske." }),
  "OP.org-cycle": () => ({
    text: "So entstünde eine Schleife in der Abteilungsstruktur.",
    hint: "Eine Abteilung kann nicht unter sich selbst oder unter einer ihrer Unterabteilungen hängen.",
  }),
  "OP.shared-org": () => ({
    text: "Dieses Schema nutzt eine geteilte Organisation.",
    hint: "Rollen, Abteilungen und Personen dort pflegen – die Änderung gilt dann für alle verknüpften Schemata.",
  }),
  "OP.no-service": () => ({ text: "Dieser Schritt hat keinen Dienst." }),
  "OP.no-staff-rule": () => ({ text: "Dieser Schritt hat keine Bearbeiterzuordnung." }),
  "OP.no-subprocess": () => ({ text: "Dieser Schritt ist an keinen Teilprozess gebunden." }),
  "OP.automation-needs-service": () => ({
    text: "Für eine Automatik braucht der Schritt zuerst einen Dienst.",
    hint: "Unter „Dienst“ einen Dienst zuweisen, dann die Automatik einstellen.",
  }),
  "OP.wrong-node-kind": (p) => ({ text: OP_WRONG_KIND[p.what] || "Das geht an diesem Knoten nicht." }),
  "M5.adhoc": () => ({
    text: "Diese Instanz wurde einzeln angepasst (Ad-hoc-Änderung) und wird deshalb nicht automatisch migriert.",
    hint: "Sie läuft sicher auf ihrer Version weiter.",
  }),
  // --- Jeder Befund des Kerns traegt einen
  // Code. IDs in den Parametern loest ``nm()`` gegen das gezeigte Modell auf,
  // ``p.step`` ergaenzt ``findingText`` aus ``node_id``.
  "K2.degree": (p) => ({
    text: `${stepOf(p)} hat ${p.in} ${Number(p.in) === 1 ? "Eingang" : "Eingänge"} und ${p.out} ${Number(p.out) === 1 ? "Ausgang" : "Ausgänge"}; erwartet ist ${degreeText(p.expected)}.`,
    hint: "Typisch bei importiertem BPMN: Verbindungen fehlen oder ein Schritt hat mehrere Ein- oder Ausgänge.",
  }),
  "K4.unknown-node": () => ({ text: "Eine Warte-Beziehung verweist auf einen Schritt, den es nicht gibt." }),
  "K4.not-activity": () => ({ text: "Warte-Beziehungen verbinden nur Aufgaben-Schritte." }),
  "K4.not-parallel": () => ({
    text: "Eine Warte-Beziehung verbindet nur Schritte verschiedener Zweige derselben parallelen Verzweigung.",
  }),
  "K4.cycle": () => ({ text: "Die Warte-Beziehungen bilden mit dem Ablauf einen Kreis – die Schritte würden aufeinander warten." }),
  "K6.decision-not-loop-end": () => ({ text: "Eine Wiederholungsbedingung hängt an einem Knoten, der kein Schleifenende ist." }),
  "K6.unbalanced": (p) => ({ text: `Es gibt ${p.starts} Schleifenanfänge, aber ${p.ends} Schleifenenden.` }),
  "K6.start-unpaired": () => ({ text: "Zu einem Schleifenanfang gibt es kein passendes Schleifenende." }),
  "K6.end-unpaired": () => ({ text: "Zu einem Schleifenende gibt es keinen passenden Schleifenanfang." }),
  "K6.end-claimed-twice": () => ({ text: "Zwei Schleifenanfänge enden am selben Schleifenende – die Schleifen überkreuzen sich." }),
  "K6.empty-body": () => ({ text: "Die Schleife enthält keinen Schritt.", hint: "Einen Schritt in die Schleife legen oder sie entfernen." }),
  "K6.no-decision": () => ({ text: "Das Schleifenende hat keine Wiederholungsbedingung.", hint: "Am Schleifenende festlegen, wann wiederholt wird." }),
  "K6.discriminator-missing": (p) => ({ text: `Das Merkmal „${nm(p.element)}“ der Wiederholungsbedingung gibt es nicht.` }),
  "K6.discriminator-not-instance": (p) => ({ text: `Das Merkmal „${nm(p.element)}“ muss ein Vorgangsdatum sein, kein externes Datum.` }),
  "K6.discriminator-not-boolean": (p) => ({ text: `Das Merkmal „${nm(p.element)}“ muss Ja/Nein sein (ist ${typeName(p.type)}).` }),
  "K6.cells-need-boolean": () => ({ text: "Eine Wiederholungsbedingung ohne Wertbereiche braucht ein Ja/Nein-Merkmal." }),
  "K6.discriminator-type": (p) => ({
    text: `${merkmalOf(p)} vom Typ ${typeName(p.type)} kann über keine Wiederholung entscheiden.`,
    hint: "Erst die Wiederholungsbedingung auf ein anderes Merkmal umstellen.",
  }),
  "K6.kind-mismatch": (p) => ({
    text: `Die Wiederholungsbedingung passt nicht zum Typ ${typeName(p.type)} von ${merkmalOf(p, "ihrem Merkmal")}.`,
    hint: "Erst die Wiederholungsbedingung anpassen, dann den Typ ändern.",
  }),
  "K6.no-exit-cell": () => ({ text: "Die Wiederholungsbedingung hat keinen Wert, bei dem die Schleife endet – sie liefe endlos." }),
  "K6.no-repeat-cell": () => ({ text: "Die Wiederholungsbedingung hat keinen Wert, bei dem wiederholt wird." }),
  "K6.max-iterations": (p) => ({ text: `Die Höchstzahl der Durchläufe muss mindestens 2 sein (ist ${p.value}).` }),
  "K6.discriminator-not-written": (p) => ({
    text: `„${nm(p.element)}“ wird nicht in jedem Durchlauf der Schleife neu gesetzt – die Schleife entschiede auf altem Stand.`,
    hint: `In der Schleife einen Schritt „${nm(p.element)}“ schreiben lassen (Pflichtbindung).`,
  }),
  "K7.decision-not-split": () => ({ text: "Eine Verzweigungsbedingung hängt an einem Knoten, der keine Entscheidung ist." }),
  "K7.condition-not-on-split": () => ({ text: "Nur Verbindungen, die eine Entscheidung verlassen, können eine Bedingung tragen." }),
  "K7.no-decision": (p) => ({
    text: `${stepOf(p, "Die Entscheidung")} hat keine Bedingung, nach der ein Zweig gewählt wird.`,
    hint: "Ein Merkmal und die Wertbereiche der Zweige festlegen.",
  }),
  "K7.targets-mismatch": () => ({ text: "Die Zweige der Bedingung passen nicht zu den Ausgängen der Entscheidung." }),
  "K7.too-few-branches": () => ({ text: "Eine Entscheidung braucht mindestens zwei Zweige." }),
  "K7.two-empty-branches": () => ({ text: "Eine Entscheidung darf höchstens einen leeren Zweig haben." }),
  // Diese Befunde kommen meist aus einer abgelehnten Aenderung am Merkmal
  // (Loeschen, Typwechsel): Sie nennen deshalb Merkmal *und* Verzweigung und
  // sagen, was zuerst zu tun ist -- nicht den hypothetischen Folgezustand.
  "K7.discriminator-missing": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)} – ohne dieses Merkmal kann dort nicht entschieden werden.`,
    hint: "Erst die Entscheidung auf ein anderes Merkmal umstellen oder die Verzweigung entfernen.",
  }),
  "K7.discriminator-not-instance": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)} und muss deshalb ein Vorgangsdatum sein, kein externes Datum.`,
  }),
  "K7.discriminator-type": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)}, kann das als ${typeName(p.type)} aber nicht.`,
    hint: "Erst die Entscheidung auf ein anderes Merkmal umstellen.",
  }),
  "K7.kind-mismatch": (p) => ({
    text: `${merkmalOf(p, "Das Merkmal")} steuert ${branchOf(p)}; deren Bedingung passt nicht zum Typ ${typeName(p.type)}.`,
    hint: "Erst die Bedingung der Entscheidung anpassen, dann den Typ ändern.",
  }),
  "K7.discriminator-unset": (p) => ({
    text: `Das Merkmal von ${stepOf(p, "der Entscheidung")} ist nicht auf jedem Weg gesetzt, wenn entschieden wird.`,
    hint: "Das Merkmal vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "K7.threshold-last-unbounded": () => ({ text: "Der letzte Wertbereich muss nach oben offen sein." }),
  "K7.threshold-only-last-unbounded": () => ({ text: "Nur der letzte Wertbereich darf nach oben offen sein." }),
  "K7.threshold-ascending": () => ({ text: "Die Grenzen der Wertbereiche müssen aufsteigen." }),
  "K7.boolean-two-branches": () => ({ text: "Eine Ja/Nein-Bedingung hat genau zwei Zweige." }),
  "K7.boolean-cover": () => ({ text: "Eine Ja/Nein-Bedingung braucht genau einen Ja- und einen Nein-Zweig." }),
  "K7.enum-one-otherwise": () => ({ text: "Eine Auswahl-Bedingung braucht genau einen Zweig „sonst“." }),
  "K7.otherwise-values": () => ({ text: "Der Zweig „sonst“ darf keine Werte aufzählen." }),
  "K7.enum-empty-branch": () => ({ text: "Jeder Zweig einer Auswahl-Bedingung braucht mindestens einen Wert." }),
  "K7.enum-duplicate": (p) => ({ text: `Der Wert ${p.value} steht in mehr als einem Zweig.` }),
  "D3.param-type": (p) => ({ text: `Der Parametertyp ${typeName(p.param_type)} passt nicht zu „${nm(p.element)}“ (${typeName(p.type)}).` }),
  "D4.unknown-node": () => ({ text: "Eine Datenbindung verweist auf einen Schritt, den es nicht gibt." }),
  "D4.not-activity": () => ({ text: "Daten lassen sich nur an Aufgaben-Schritte binden." }),
  "D4.unknown-element": (p) => ({ text: `Eine Datenbindung verweist auf das unbekannte Datenelement „${nm(p.element)}“.` }),
  "U1.unknown-node": () => ({ text: "Eine Eingabemaske hängt an einem Schritt, den es nicht gibt." }),
  "U1.not-activity": () => ({ text: "Eingabemasken gibt es nur an Aufgaben-Schritten." }),
  "U1.unknown-element": (p) => ({ text: `Ein Maskenfeld verweist auf das unbekannte Datenelement „${nm(p.element)}“.` }),
  "U2.duplicate-field": () => ({ text: "Zwei Felder der Maske haben dieselbe Kennung." }),
  "U2.element-twice": (p) => ({ text: `„${nm(p.element)}“ steht zweimal in derselben Maske.` }),
  "U2.empty-label": () => ({ text: "Ein Maskenfeld hat keine Beschriftung." }),
  "U2.widget-type": (p) => ({
    text: `In der Maske von ${stepOf(p, "einem Schritt")} kann das Bedienelement „${nm(p.element)}“ (${typeName(p.type)}) nicht darstellen.`,
    hint: "Das Feld in der Maske auf ein passendes Bedienelement umstellen oder entfernen.",
  }),
  "U2.dropdown-options": () => ({ text: "Eine Auswahlliste braucht mindestens zwei Einträge." }),
  "U2.duplicate-options": () => ({ text: "Eine Auswahlliste enthält einen Eintrag doppelt." }),
  "U2.options-not-dropdown": () => ({ text: "Nur eine Auswahlliste trägt Einträge." }),
  "U3.input-no-write": (p) => ({ text: `Das Eingabefeld für „${nm(p.element)}“ hat keine Schreibbindung am Schritt.` }),
  "U3.display-no-read": (p) => ({ text: `Das Anzeigefeld für „${nm(p.element)}“ hat keine Lesebindung am Schritt.` }),
  "C1.instance-with-binding": (p) => ({ text: `„${nm(p.element)}“ ist ein Vorgangsdatum und darf keine externe Anbindung tragen.` }),
  "C1.binding-kinds": (p) => ({ text: `„${nm(p.element)}“ braucht genau eine Art externer Anbindung.` }),
  "C1.binding-missing": (p) => ({ text: `Das externe Datenelement „${nm(p.element)}“ hat keine Anbindung.` }),
  "C1.unknown-connector": (p) => ({ text: `„${nm(p.element)}“ verweist auf den unbekannten Connector „${p.connector}“.` }),
  "C3.empty-entity": (p) => ({ text: `Für „${nm(p.element)}“ fehlt die Tabelle.` }),
  "C2.self-key": (p) => ({ text: `„${nm(p.element)}“ kann nicht sein eigener Schlüssel sein.` }),
  "C2.unknown-key": (p) => ({ text: `Der Schlüssel „${nm(p.key)}“ von „${nm(p.element)}“ existiert nicht.` }),
  "C2.key-not-instance": (p) => ({ text: `Der Schlüssel „${nm(p.key)}“ von „${nm(p.element)}“ muss ein Vorgangsdatum sein.` }),
  "C2.key-not-set": (p) => ({
    text: `Der Schlüssel „${nm(p.key)}“ für „${nm(p.element)}“ ist nicht auf jedem Weg gesetzt, bevor gelesen wird.`,
    hint: "Den Schlüssel vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "C4.type-mismatch": (p) => ({ text: `„${nm(p.element)}“ ist ${typeName(p.type)}, die Abfrage liefert aber ${typeName(p.result_type)}.` }),
  "C5.unknown-connector": (p) => ({ text: `„${nm(p.element)}“ verweist auf den unbekannten Connector „${p.connector}“.` }),
  "C5.empty-entity": (p) => ({ text: `Für die Abfrage von „${nm(p.element)}“ fehlt die Tabelle.` }),
  "C5.empty-column": (p) => ({ text: `Für die Abfrage von „${nm(p.element)}“ fehlt die Spalte.` }),
  "C5.operator-type": (p) => ({ text: `Der Vergleich ${p.operator} passt nicht zu einer Spalte vom Typ ${typeName(p.column_type)} (${nm(p.element)}).` }),
  "C5.unknown-source": (p) => ({ text: `Ein Filter von „${nm(p.element)}“ verweist auf das unbekannte Datenelement „${nm(p.source)}“.` }),
  "C5.source-not-instance": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ für „${nm(p.element)}“ muss ein Vorgangsdatum sein.` }),
  "C5.source-type": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ (${typeName(p.source_type)}) für „${nm(p.element)}“ passt nicht zur Spalte (${typeName(p.column_type)}).` }),
  "C5.source-unset": (p) => ({
    text: `Der Filterwert „${nm(p.source)}“ für „${nm(p.element)}“ ist nicht auf jedem Weg gesetzt, bevor gelesen wird.`,
    hint: "Den Filterwert vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "C6.no-unique-column": (p) => ({ text: `„${nm(p.element)}“ liest genau einen Datensatz, nennt aber keine eindeutige Spalte.` }),
  "C6.no-unique-filter": (p) => ({ text: `„${nm(p.element)}“ liest genau einen Datensatz, filtert aber nicht auf die eindeutige Spalte „${p.column}“.` }),
  "C6.aggregate-plain-column": (p) => ({ text: `„${nm(p.element)}“ fasst Datensätze zusammen, liest aber eine einfache Spalte.` }),
  "C6.empty-order": (p) => ({ text: `„${nm(p.element)}“ nimmt den ersten Datensatz, gibt aber keine Sortierung an.` }),
  "C7.type-mismatch": (p) => ({ text: `„${nm(p.element)}“ ist ${typeName(p.type)}, die Zielspalte aber ${typeName(p.column_type)}.` }),
  "C8.unknown-connector": (p) => ({ text: `Das Zurückschreiben von „${nm(p.element)}“ verweist auf den unbekannten Connector „${p.connector}“.` }),
  "C8.empty-entity": (p) => ({ text: `Für das Zurückschreiben von „${nm(p.element)}“ fehlt die Tabelle.` }),
  "C8.empty-column": (p) => ({ text: `Für das Zurückschreiben von „${nm(p.element)}“ fehlt die Zielspalte.` }),
  "C8.operator-type": (p) => ({ text: `Der Vergleich ${p.operator} passt nicht zu einer Spalte vom Typ ${typeName(p.column_type)} (${nm(p.element)}).` }),
  "C8.unknown-source": (p) => ({ text: `Ein Filter beim Zurückschreiben von „${nm(p.element)}“ verweist auf das unbekannte Datenelement „${nm(p.source)}“.` }),
  "C8.source-not-instance": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ für „${nm(p.element)}“ muss ein Vorgangsdatum sein.` }),
  "C8.source-type": (p) => ({ text: `Der Filterwert „${nm(p.source)}“ (${typeName(p.source_type)}) für „${nm(p.element)}“ passt nicht zur Spalte (${typeName(p.column_type)}).` }),
  "C8.source-unset": (p) => ({
    text: `Der Filterwert „${nm(p.source)}“ ist nicht auf jedem Weg gesetzt, bevor „${nm(p.element)}“ zurückgeschrieben wird.`,
    hint: "Den Filterwert vorher in einem Schritt schreiben lassen (Pflichtbindung).",
  }),
  "C9.no-unique-column": (p) => ({ text: `Das Zurückschreiben von „${nm(p.element)}“ nennt keine eindeutige Spalte – es muss genau einen Datensatz treffen.` }),
  "C9.no-unique-filter": (p) => ({ text: `Das Zurückschreiben von „${nm(p.element)}“ filtert nicht auf die eindeutige Spalte „${p.column}“.` }),
  "Z1.unknown-role": (p) => ({ text: `Die Bearbeiterregel nennt die unbekannte Rolle „${nm(p.ref)}“.` }),
  "Z1.unknown-unit": (p) => ({ text: `Die Bearbeiterregel nennt die unbekannte Abteilung „${nm(p.ref)}“.` }),
  "Z1.unknown-agent": (p) => ({ text: `Die Bearbeiterregel nennt die unbekannte Person „${nm(p.ref)}“.` }),
  "Z1.unknown-node-ref": (p) => ({ text: `Die Bearbeiterregel verweist auf den unbekannten Schritt „${nm(p.ref)}“.` }),
  "Z1.unknown-node": () => ({ text: "Eine Bearbeiterregel hängt an einem Schritt, den es nicht gibt." }),
  "Z1.not-activity": () => ({ text: "Bearbeiter lassen sich nur Aufgaben-Schritten zuordnen." }),
  "Z1.no-operands-allowed": () => ({ text: "Diese Art von Bearbeiterregel darf keine Teilregeln enthalten." }),
  "Z1.reference-missing": () => ({ text: "Der Bearbeiterregel fehlt die Angabe, wer gemeint ist." }),
  "Z1.except-two": () => ({ text: "„außer“ braucht genau zwei Teilregeln: wer, und wer davon nicht." }),
  "Z1.too-few-operands": (p) => ({ text: `Diese Kombination braucht mindestens ${countLabel(Number(p.count), "Teilregel", "Teilregeln")}.` }),
  "Z1.agent-unknown-role": (p) => ({ text: `„${nm(p.agent)}“ hat die unbekannte Rolle „${nm(p.role)}“.` }),
  "Z1.agent-unknown-unit": (p) => ({ text: `„${nm(p.agent)}“ gehört zur unbekannten Abteilung „${nm(p.unit_ref)}“.` }),
  "Z1.own-deputy": (p) => ({ text: `„${nm(p.agent)}“ kann nicht die eigene Vertretung sein.` }),
  "Z1.unknown-deputy": (p) => ({ text: `Die Vertretung „${nm(p.deputy)}“ von „${nm(p.agent)}“ gibt es nicht.` }),
  "Z1.unknown-manager": (p) => ({ text: `Die Leitung „${nm(p.manager)}“ der Abteilung „${nm(p.unit)}“ gibt es nicht.` }),
  "Z1.unknown-parent": (p) => ({ text: `Die übergeordnete Abteilung „${nm(p.parent)}“ von „${nm(p.unit)}“ gibt es nicht.` }),
  "Z1.unit-cycle": () => ({ text: "Die Abteilungen sind im Kreis untergeordnet." }),
  "Z4.not-activity": () => ({ text: "Ein Dienst lässt sich nur an Aufgaben-Schritte binden." }),
  "A1.unknown-template": (p) => ({ text: `Die Dienst-Vorlage „${p.template}“ gibt es nicht.` }),
  "A2.executor-mismatch": (p) => ({ text: `„automatisch“ passt nicht zur Ausführungsart der Dienst-Vorlage „${p.template}“.` }),
  "A3.param-unbound": (p) => ({ text: `Der Pflichtparameter „${p.param}“ des Dienstes ist nicht belegt.` }),
  "A3.unknown-param": (p) => ({ text: `Die Dienst-Vorlage „${p.template}“ hat keinen Parameter „${p.param}“.` }),
  "A3.unknown-element": (p) => ({ text: `Der Parameter „${p.param}“ ist an das unbekannte Datenelement „${nm(p.element)}“ gebunden.` }),
  "A3.type-mismatch": (p) => ({ text: `Der Parameter „${p.param}“ (${typeName(p.param_type)}) passt nicht zu „${nm(p.element)}“ (${typeName(p.type)}).` }),
  "I1.no-topic": () => ({ text: "Eine externe Aufgabe braucht ein Thema (Topic)." }),
  "I1.no-endpoint": () => ({ text: "Ein HTTP-Aufruf braucht einen Endpunkt." }),
  "I2.topic-with-endpoint": () => ({ text: "Eine externe Aufgabe darf keinen Endpunkt tragen." }),
  "I2.endpoint-with-topic": () => ({ text: "Ein HTTP-Aufruf darf kein Thema (Topic) tragen." }),
  "I2.automated-not-automatic": () => ({ text: "Eine angebundene Automatik muss als „automatisch“ markiert sein." }),
  "I3.unknown-element": (p) => ({ text: `Der Parameter „${p.param}“ verweist auf das unbekannte Datenelement „${nm(p.element)}“.` }),
  "I4.inline-reference": () => ({
    text: "Adressen und Zugangsdaten gehören nicht ins Modell, nur ein Verweis auf die Konfiguration.",
  }),
  "T1.negative-deadline": () => ({ text: "Der Termin des Prozesses darf nicht negativ sein." }),
  "T1.unknown-node": () => ({ text: "Eine Zeitvorgabe hängt an einem Schritt, den es nicht gibt." }),
  "T1.negative-duration": () => ({ text: "Die Höchstdauer darf nicht negativ sein." }),
  "T1.negative-lead": () => ({ text: "Die Soll-Zeit darf nicht negativ sein." }),
  "T3.not-activity": () => ({ text: "Eine Eskalation gibt es nur an Aufgaben-Schritten." }),
  "T3.automatic": () => ({ text: "Ein automatischer Schritt eskaliert nicht – dafür gibt es Störungen und Wiederholungen." }),
  "T3.no-target-time": () => ({ text: "Für eine Eskalation braucht der Schritt eine Soll-Zeit oder Höchstdauer." }),
  "T3.no-stages": () => ({ text: "Eine Eskalation braucht mindestens eine Stufe." }),
  "T3.negative-offset": () => ({ text: "Eine Eskalationsstufe darf nicht vor der Fälligkeit liegen." }),
  "T3.offsets-ascending": () => ({ text: "Die Eskalationsstufen müssen zeitlich aufeinander folgen." }),
  "T3.node-ref-target": () => ({ text: "Eine Eskalation richtet sich an Rollen oder Abteilungen, nicht an den Bearbeiter eines Schritts." }),
  "T3.nobody": () => ({ text: "Das Ziel der Eskalation findet niemanden im Organisationsmodell." }),
  "N1.agent-mail": (p) => ({ text: `Die E-Mail-Adresse von „${nm(p.agent)}“ ist ungültig.` }),
  "N1.role-mail": (p) => ({ text: `Das Gruppenpostfach der Rolle „${nm(p.role)}“ ist ungültig.` }),
  "N1.unit-mail": (p) => ({ text: `Das Postfach der Abteilung „${nm(p.unit)}“ ist ungültig.` }),
  "N2.unknown-node": () => ({ text: "Eine E-Mail-Benachrichtigung hängt an einem Schritt, den es nicht gibt." }),
  "N2.not-activity": () => ({ text: "E-Mail-Benachrichtigungen gibt es nur an Aufgaben-Schritten." }),
  "N2.no-staff-rule": () => ({ text: "Ohne Bearbeiterzuordnung gibt es niemanden, der benachrichtigt werden kann." }),
  "N3.not-static": () => ({
    text: "Die Empfänger hängen vom Bearbeiter eines früheren Schritts ab – persönliche Mails lassen sich hier nicht modellieren.",
    hint: "Ein Gruppenpostfach verwenden.",
  }),
  "N3.no-address": (p) => ({ text: `„${nm(p.agent)}“ könnte zuständig sein, hat aber keine E-Mail-Adresse.` }),
  "N3.no-group": () => ({ text: "Die Bearbeiterregel nennt keine Rolle oder Abteilung – es gibt kein Gruppenpostfach." }),
  "N3.role-no-mailbox": (p) => ({ text: `Die Rolle „${nm(p.ref)}“ hat kein Gruppenpostfach.` }),
  "N3.unit-no-mailbox": (p) => ({ text: `Die Abteilung „${nm(p.ref)}“ hat kein Postfach.` }),
  "N3.performer-no-mailbox": () => ({ text: "Der Bearbeiter eines früheren Schritts hat kein Gruppenpostfach – dafür persönliche Mails verwenden." }),
  "N4.unknown-element": (p) => ({ text: `Der Platzhalter {${p.ref}} verweist auf ein unbekanntes Datenelement.` }),
  "N4.not-instance": (p) => ({ text: `Der Platzhalter {${p.ref}} ist kein Vorgangsdatum und steht im Mailtext nicht zur Verfügung.` }),
  "N4.not-set": (p) => ({ text: `Der Platzhalter {${p.ref}} ist nicht sicher gesetzt, wenn die Aufgabe bereit wird.` }),
  "H1.no-binding": () => ({ text: "Der Teilprozess-Schritt ist an keinen Teilprozess gebunden." }),
  "H1.not-subprocess": () => ({ text: "Eine Teilprozess-Bindung hängt an einem Schritt, der kein Teilprozess ist." }),
  "H1.target-missing": (p) => ({ text: `Den Teilprozess „${schemaName(p.target)}“ (Version ${p.version}) gibt es nicht.` }),
  "H1.target-not-released": (p) => ({ text: `Der Teilprozess „${schemaName(p.target)}“ ist nicht freigegeben.` }),
  "H2.unknown-parent-element": (p) => ({ text: `Die Übergabe verweist auf das unbekannte Datenelement „${nm(p.parent_element)}“.` }),
  "H2.unknown-target-element": (p) => ({ text: `Die Übergabe nennt das unbekannte Datenelement „${p.target_element}“ im Teilprozess.` }),
  "H2.type-mismatch": (p) => ({ text: `Die Übergabe verbindet „${nm(p.parent_element)}“ (${typeName(p.parent_type)}) mit „${p.target_element}“ (${typeName(p.target_type)}).` }),
  "H2.output-not-written": (p) => ({ text: `Der Teilprozess setzt „${p.target_element}“ nicht auf jedem Weg – „${nm(p.parent_element)}“ bliebe leer.` }),
  "H2.input-not-written": (p) => ({ text: `„${nm(p.parent_element)}“ ist nicht auf jedem Weg gesetzt, bevor der Teilprozess startet.` }),
  "H3.cycle": () => ({ text: "Die Teilprozesse enthalten sich gegenseitig – ein Prozess kann sich nicht selbst enthalten." }),
  "F1.no-released-version": () => ({ text: "Vom Folgeprozess gibt es keine passende freigegebene Version." }),
  "F1.not-released": () => ({ text: "Der Folgeprozess ist nicht freigegeben." }),
  "F2.unknown-source": (p) => ({ text: `Die Übergabe an den Folgeprozess verweist auf das unbekannte Datenelement „${nm(p.source_element)}“.` }),
  "F2.unknown-target": (p) => ({ text: `Die Übergabe nennt das unbekannte Datenelement „${p.target_element}“ im Folgeprozess.` }),
  "F2.type-mismatch": (p) => ({ text: `Die Übergabe verbindet „${nm(p.source_element)}“ (${typeName(p.source_type)}) mit „${p.target_element}“ (${typeName(p.target_type)}).` }),
  "F4.no-condition": () => ({ text: "Ein bedingter Folgeprozess hat keine Bedingung." }),
  "F4.invalid-condition": (p) => ({ text: `Die Bedingung des Folgeprozesses ist ungültig: ${p.error}` }),
  "F4.unknown-element": (p) => ({ text: `Die Bedingung des Folgeprozesses verweist auf das unbekannte Datenelement „${p.element}“.` }),
  "F4.condition-not-written": (p) => ({
    text: `Die Bedingung des Folgeprozesses liest „${nm(p.element)}“, das nicht auf jedem Weg gesetzt wird.`,
    hint: "Das Datenelement in einem Schritt auf jedem Weg schreiben lassen, z. B. über ein Ankreuzfeld.",
  }),
  "R1.not-found": () => ({ text: "Diesen Schritt gibt es in dem Vorgang nicht." }),
  "R1.after-end": () => ({ text: "Hinter dem Ende lässt sich nichts einfügen." }),
  "R1.anchor-not-serial": (p) => ({ text: `Hinter ${stepOf(p, "diesem Knoten")} lässt sich nichts einfügen – er hat mehr als einen Ausgang.` }),
  "R1.already-passed": (p) => ({ text: `Der Vorgang ist schon über ${stepOf(p, "diese Stelle")} hinaus – dort lässt sich nichts mehr ändern.` }),
  "R1.already-reached": (p) => ({ text: `${stepOf(p)} ist im Vorgang schon erreicht – ad hoc ändern lässt sich nur, was der Vorgang noch nicht erreicht hat.` }),
  "R1.delete-not-activity": () => ({ text: "Ad hoc lassen sich nur Aufgaben-Schritte entfernen." }),
  "R1.not-serial": () => ({ text: "Dieser Schritt liegt nicht auf einer einfachen Strecke und lässt sich ad hoc nicht entfernen." }),
  "R1.rename-not-step": () => ({ text: "Ad hoc lassen sich nur Schritte und Teilprozesse umbenennen." }),
  "OP.own-deputy": () => ({ text: "Eine Person kann nicht ihre eigene Vertretung sein." }),
  "OP.label-empty": (p) => ({ text: `${OP_KIND_NAMES[p.kind] || "Das Element"} braucht einen Namen.` }),
  "OP.label-too-long": (p) => ({ text: `Der Name ist zu lang – höchstens ${p.max} Zeichen.` }),
  "U6.label-too-long": (p) => ({ text: `${stepOf(p, "Eine Bezeichnung")} ist zu lang – höchstens ${p.max} Zeichen.` }),
  "U2.bounds-not-number": (p) => ({ text: `„${p.field}“: Unter- und Obergrenze gibt es nur bei Zahlenfeldern.` }),
  "U2.bounds-order": (p) => ({ text: `„${p.field}“: Die Untergrenze liegt über der Obergrenze.` }),
  "U2.text-rule-not-text": (p) => ({ text: `„${p.field}“: Muster und Höchstlänge gibt es nur bei Textfeldern.` }),
  "U2.pattern-invalid": (p) => ({ text: `„${p.field}“: Das Muster ist kein gültiger regulärer Ausdruck.` }),
  "U2.length-invalid": (p) => ({ text: `„${p.field}“: Die Höchstlänge muss mindestens 1 sein.` }),
  "U4.below-min": (p) => ({ text: `„${p.field}“ muss mindestens ${p.min} sein.` }),
  "U4.above-max": (p) => ({ text: `„${p.field}“ darf höchstens ${p.max} sein.` }),
  "U4.too-long": (p) => ({ text: `„${p.field}“ darf höchstens ${p.max} Zeichen lang sein.` }),
  "U4.pattern": (p) => ({ text: `„${p.field}“ passt nicht zum vorgegebenen Format.` }),
  "U5.too-many": (p) => ({ text: `Höchstens ${p.max} Datenelemente können einen Vorgang benennen.` }),
  "U5.unknown-element": (p) => ({ text: `Das Datenelement „${nm(p.element)}“ zur Benennung des Vorgangs gibt es nicht.` }),
  "U5.not-instance": (p) => ({ text: `„${p.element}“ ist ein externes Datum und kann den Vorgang nicht benennen.` }),
};

/**
 * Anzeigetext eines Befunds: aus dem Katalog, sonst die Kernmeldung.
 *
 * Im Rueckfall wird eine enthaltene Knoten-ID durch den Schrittnamen ersetzt,
 * soweit das geladene Schema ihn kennt -- so erscheint auch ein noch nicht
 * uebersetzter Befund nicht mit „act_246“.
 *
 * @param {{rule:string, message:string, node_id?:string|null, code?:string|null, params?:Object<string,string>}} f Befund
 * @param {{withHint?: boolean}} [opts] ``withHint`` haengt den Handlungsvorschlag an
 * @returns {string}
 */
/**
 * Anzeigename fuer eine ID aus Befund-Parametern.
 *
 * Viele Befunde des Kerns nennen IDs (Datenelement, Rolle, Person, Schritt),
 * weil der Kern an der Stelle nur die ID kennt. Aufgeloest wird gegen das
 * gezeigte Modell: Schritte (auch im Ad-hoc-Wandel des Vorgangs),
 * Datenelemente, Rollen, Abteilungen, Personen. Unbenannte Knoten heissen
 * nach ihrem Zusammenhang (``nodeCaptionInContext``). Unbekanntes bleibt, wie es
 * ist -- ein Name aus dem Kern loest sich so auf sich selbst auf.
 * @param {string|undefined} id ID oder bereits ein Name
 * @returns {string}
 */
function nm(id) {
  if (id == null || id === "") return "\u2013";
  const models = [state.schema, state.instance && state.instance.ad_hoc_schema].filter(Boolean);
  for (const m of models) {
    const n = (m.nodes || {})[id];
    // Unbenannte Verzweigungen hiessen sonst alle „XOR ▶“ -- im Befund
    // unbrauchbar; mit Zusammenhang „Entscheidung nach „Betrag erfassen““.
    if (n) return nodeCaptionInContext(m, n);
    const d = (m.data_elements || {})[id];
    if (d) return d.name || id;
    const org = m.org_model || {};
    for (const map of [org.roles, org.org_units, org.agents]) {
      if (map && map[id]) return map[id].name || id;
    }
  }
  if (state.agentDirectory && state.agentDirectory[id]) return state.agentDirectory[id].name || id;
  return String(id);
}

/**
 * Fachliche Namen der Datentypen fuer **jede** Anzeige.
 * INTEGER/FLOAT/STRING/BOOLEAN/URI standen roh in Dialogen und Tabellen; die
 * API-Werte bleiben unveraendert (``value`` der Auswahllisten), nur die
 * Beschriftung ist deutsch.
 */
const DATA_TYPE_LABELS = {
  INTEGER: "Ganzzahl", FLOAT: "Kommazahl", DECIMAL: "Betrag", STRING: "Text", DATE: "Datum", BOOLEAN: "Ja/Nein", URI: "Link",
};
function typeName(t) { return DATA_TYPE_LABELS[t] || t || "?"; }

/**
 * Titel eines Vorgangs aus seinen benennenden Werten (``display_fields``):
 * „Müller GmbH · Auftragswert: 8.880,00“ statt ``instance_14``. Texte stehen
 * pur; Zahlen und Ja/Nein tragen den Feldnamen davor, sonst sagt eine nackte
 * „8“ nichts. Formatiert wird nach Datentyp (``formatValue``): Betrag mit zwei
 * Stellen, Ganzzahl ohne Tausenderpunkt (Kennnummern). Leere Werte fallen
 * weg; ohne Werte ist der Titel leer und die Aufrufer fallen auf
 * ``instanceName`` (Startzeit) zurueck.
 * @param {{name: string, value: *, data_type?: string}[]|undefined} values
 *   ``context`` einer Aufgabe bzw. ein Eintrag aus ``GET /instance-titles``
 * @returns {string}
 */
function contextTitle(values) {
  return (values || [])
    .filter((v) => v.value !== null && v.value !== undefined && v.value !== "")
    .map((v) => {
      if (typeof v.value === "string") return v.value;
      const shown = formatValue({ data_type: v.data_type }, v.value);
      return v.name ? `${v.name}: ${shown}` : shown;
    })
    .join(" \u00B7 ");
}

/**
 * Text eines Modellhinweises (G-Gruppe) ohne interne Kennung.
 *
 * Der Kern formuliert Hinweise schon deutsch, nennt den Knoten darin aber per
 * Kennung („Das Gateway 'split_482' …“). Hier wird sie durch den lesbaren Namen
 * ersetzt -- bei unbenannten Verzweigungen „Entscheidung nach „Betrag
 * erfassen““ (``nodeCaptionInContext``). Hinweise sind beratend; die
 * Darstellung bleibt neutral (Klasse ``rule-hint``), nie rot wie ein Befund.
 *
 * @param {{code: string, message: string, node_id?: string|null}} h Hinweis
 * @param {object} schema Schema, auf das er sich bezieht
 * @returns {string}
 */
function hintText(h, schema) {
  const msg = (h && h.message) || "";
  const node = h && h.node_id && schema && (schema.nodes || {})[h.node_id];
  if (!node) return msg;
  return msg.split(`'${h.node_id}'`).join(`\u201E${nodeCaptionInContext(schema, node)}\u201C`);
}

/**
 * Lesbarer Name eines Vorgangs -- nie die interne Kennung allein.
 *
 * Reihenfolge: die benennenden Werte (``display_fields``, z. B. „Müller GmbH
 * · A-4711“); fehlen sie (noch -- beim ersten Schritt sind sie meist leer),
 * „Vorgang vom 01.10., 14:03“ aus der Startzeit; ohne Startzeit (Altbestand)
 * schlicht „Vorgang“. Die Kennung zeigt ``instanceNameCell`` klein darunter.
 *
 * @param {string|null|undefined} startedAt ISO-Zeitpunkt des Starts
 * @param {{name: string, value: *}[]|undefined} values benennende Werte
 * @returns {string}
 */
function instanceName(startedAt, values) {
  const title = contextTitle(values);
  if (title) return title;
  const d = startedAt ? new Date(startedAt) : null;
  if (d && !isNaN(d.getTime())) {
    const when = d.toLocaleString("de-DE",
      { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    return `Vorgang vom ${when}`;
  }
  return "Vorgang";
}

/**
 * Tabellenzelle bzw. Kopfzeile fuer einen Vorgang: Name gross, Kennung klein
 * darunter (fuer Rueckfragen und den Bezug zum Audit-Log), Kennung auch im
 * Tooltip.
 * @param {string} id Kennung des Vorgangs
 * @param {string|null|undefined} startedAt Startzeit (ISO)
 * @param {{name: string, value: *}[]|undefined} values benennende Werte
 * @returns {HTMLElement}
 */
function instanceNameCell(id, startedAt, values) {
  return el("div", { title: `Kennung: ${id}` }, instanceName(startedAt, values),
    el("div", { class: "task-context" }, id));
}

/**
 * Benennende Werte eines geladenen Vorgangs gemaess seinem Schema.
 * @param {object} inst Vorgang (mit ``data_values``)
 * @param {object} schema Schema, gegen das er laeuft
 * @returns {{name: string, value: *}[]}
 */
function instanceValues(inst, schema) {
  const elems = (schema && schema.data_elements) || {};
  return ((schema && schema.display_fields) || []).map((eid) => ({
    name: (elems[eid] && elems[eid].name) || eid, value: (inst.data_values || {})[eid],
    data_type: elems[eid] && elems[eid].data_type,
  }));
}

/**
 * Anzeigename eines Vorgangs in seiner Detailansicht (ohne Kennung -- die
 * steht im Tooltip der Aufrufer).
 * @param {object} inst Vorgang
 * @param {object} schema Schema, gegen das er laeuft
 * @returns {string}
 */
function instanceCaption(inst, schema) {
  return instanceName(inst.started_at, instanceValues(inst, schema));
}

/** Name eines Schemas (Teilprozess, Folgeprozess) aus seiner ID, ohne Version. */
function schemaName(id) { return (state.schemaNames && state.schemaNames[id]) || id; }

/**
 * Der Schritt eines Befunds als Satzanfang: „„Pruefen““ oder ein Ersatzwort.
 * @param {object} p Parameter (``step`` ergaenzt ``findingText`` aus ``node_id``)
 * @param {string} [fallback] Ersatz, wenn der Befund keinen Schritt nennt
 */
/**
 * Deutscher Name eines BPMN-Elements, das der Import ablehnt.
 * @param {string} element lokaler Elementname, z. B. "inclusiveGateway"
 * @returns {string} Name mit dem BPMN-Begriff in Klammern
 */
function bpmnElementName(element) {
  const names = {
    inclusiveGateway: "ODER-Gateway (inclusiveGateway)",
    eventBasedGateway: "ereignisbasiertes Gateway (eventBasedGateway)",
    complexGateway: "komplexes Gateway (complexGateway)",
    intermediateCatchEvent: "Zwischenereignis (intermediateCatchEvent)",
    intermediateThrowEvent: "ausl\u00F6sendes Zwischenereignis (intermediateThrowEvent)",
    boundaryEvent: "angeheftetes Ereignis (boundaryEvent)",
    transaction: "Transaktion (transaction)",
    adHocSubProcess: "Ad-hoc-Teilprozess (adHocSubProcess)",
  };
  return names[element] || element || "?";
}

function stepOf(p, fallback) { return p.step ? `„${p.step}“` : (fallback || "Dieser Schritt"); }

/**
 * Das Merkmal eines Entscheidungs- oder Schleifenbefunds als Satzteil.
 *
 * Der Kern liefert die Element-ID in ``p.element``; ``nm`` loest sie gegen das
 * gezeigte Modell auf. Nach einer abgelehnten Loeschung steht das Element dort
 * noch -- so erscheint der Name, nicht der hypothetische Folgezustand.
 * @param {object} p Befund-Parameter
 * @param {string} [fallback] Satzteil, wenn der Kern kein Element nennt
 *   (aeltere Befunde); Standard „Das Merkmal“
 * @returns {string} z. B. „Betrag“ in deutschen Anfuehrungszeichen
 */
function merkmalOf(p, fallback) { return p.element ? `„${nm(p.element)}“` : (fallback || "Das Merkmal"); }

/**
 * Die betroffene Verzweigung eines K7-Befunds als Satzteil.
 *
 * Benannt: „die Verzweigung „Betrag hoch?““. Unbenannt traegt ``p.step`` schon
 * den Zusammenhang („Entscheidung nach „Betrag erfassen““, siehe ``nm``) und
 * wird nur mit Artikel versehen -- sonst stuende das Wort doppelt und die
 * Anfuehrungszeichen geschachtelt. ``p.step``/``p._nodeId`` ergaenzt
 * ``findingText`` aus ``node_id``.
 * @param {object} p Befund-Parameter
 * @returns {string} Satzteil ohne Satzzeichen; ohne Schritt „eine Verzweigung“
 */
function branchOf(p) {
  if (!p.step) return "eine Verzweigung";
  const node = p._nodeId && state.schema && (state.schema.nodes || {})[p._nodeId];
  if (node && !(node.label || "").trim()) return `die ${p.step}`;
  return `die Verzweigung „${p.step}“`;
}

/** „in=1, out>=2“ aus K2 als Satzteil. */
function degreeText(expected) {
  const m = /in(>=|=)(\d+), out(>=|=)(\d+)/.exec(expected || "");
  if (!m) return expected || "?";
  const q = (op, n, one, many) => `${op === ">=" ? "mindestens " : "genau "}${countLabel(Number(n), one, many)}`;
  return `${q(m[1], m[2], "Eingang", "Eingänge")} und ${q(m[3], m[4], "Ausgang", "Ausgänge")}`;
}

function findingText(f, opts) {
  const entry = f && f.code && FINDING_TEXTS[f.code];
  if (entry) {
    // ``step`` ergaenzen, wo der Kern nur ``node_id`` mitgibt.
    const params = Object.assign({}, f.params || {});
    if (!params.step && f.node_id) params.step = nm(f.node_id);
    // Fuer Satzteile, die die Knotenart brauchen (``branchOf``).
    if (f.node_id) params._nodeId = f.node_id;
    const t = entry(params);
    return opts && opts.withHint && t.hint ? `${t.text} ${t.hint}` : t.text;
  }
  let msg = (f && f.message) || "";
  const nodes = state.schema && state.schema.nodes;
  if (nodes) {
    msg = msg.replace(/'([A-Za-z0-9_-]+)'/g, (m, id) =>
      nodes[id] && nodes[id].label ? `„${nodes[id].label}“` : m);
  }
  return msg;
}

/** Befundzeile fuer Listen und Meldungen: „Regel – Text“ (Regel bleibt als Kuerzel stehen). */
function findingLine(f, opts) {
  return `${f.rule}: ${findingText(f, opts)}`;
}

function describeError(err) {
  // err.detail can be: string, {message}, {findings:[{rule,message,node_id,code,params}]}
  const d = err && err.detail;
  if (!d) return { title: err.message || "Fehler", lines: [] };
  // 403 der Rollenpruefung kommt als nacktes „forbidden“.
  if (err.status === 403 && d === "forbidden") {
    return { title: "Daf\u00FCr fehlt dir die Berechtigung.", lines: ["Wende dich an deine Administration, wenn du diese Aktion brauchst."] };
  }
  if (typeof d === "string") return { title: d, lines: [] };
  if (d.findings) {
    // Titel ohne Fachjargon; die Regel steht als Kuerzel vor jeder Zeile.
    return {
      title: d.findings.length === 1 ? "Diese Änderung ist nicht zulässig" : "Diese Änderung ist nicht zulässig – mehrere Gründe",
      // Gleich lautende Zeilen nur einmal: Mehrere Befunde koennen fachlich
      // identisch formuliert sein (gleiche Regel, gleicher Bezug).
      lines: [...new Set(d.findings.map((f) => findingLine(f, { withHint: true })))],
    };
  }
  // Ein Boundary-Befund mit Code (z. B. die SSRF-Pruefung der Webhooks) wird im
  // selben Katalog formuliert wie ein Regelbefund -- sonst stuende hier wieder
  // ein englischer Satz.
  if (d.code) return { title: findingText(d, { withHint: true }), lines: [] };
  if (d.message) return { title: d.message, lines: [] };
  return { title: "Fehler", lines: [] };
}

/**
 * Zeigt einen gescheiterten API-Aufruf als Fehlermeldung an.
 *
 * Buendelt das Muster ``describeError`` + ``toast("err", …)``, das fast jede
 * Aktion in ihrem ``catch`` braucht. Formuliert wird ausschliesslich in
 * ``describeError`` (Regelbefunde ueber ``findingText``, 403 als Satz); diese
 * Funktion reicht Titel und Zeilen nur unveraendert an ``toast`` weiter.
 *
 * @param {*} err Der gefangene Fehler (typisch ``{status, detail}`` aus
 *   ``request``, aber auch ein gewoehnlicher ``Error``).
 * @returns {void}
 */
function toastError(err) {
  const d = describeError(err);
  toast("err", d.title, d.lines);
}

// --------------------------------------------------------------------------
// Web-Client
// --------------------------------------------------------------------------

/**
 * Der einzige Weg, auf dem dieser Client die API erreicht.
 *
 * Genau deshalb sitzt hier der Sperrpunkt der gefuehrten Tour: Solange eine
 * Sandkasten-Tour laeuft, beantwortet ``Tour.intercept`` jeden schreibenden
 * Aufruf aus einer Aufzeichnung, und es findet KEIN fetch statt. Dadurch
 * entstehen durch Tutorial-Eingaben keine dauerhaften Daten.
 * Ausserhalb der Tour ist der Aufruf ein No-op.
 *
 * @param {string} method HTTP-Methode.
 * @param {string} path Pfad ab der API-Basis.
 * @param {object} [body] Anfragekoerper (JSON).
 * @returns {Promise<*>} Antwortkoerper; wirft bei Fehlern {status, detail}.
 */
async function request(method, path, body) {
  if (typeof Tour !== "undefined") {
    const simulated = Tour.intercept(method, path, body);
    if (simulated) return simulated();
  }
  let resp;
  try {
    resp = await fetch(state.apiBase + path, {
      method,
      headers: authHeaders(body !== undefined),
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    setConnected(false);
    throw { detail: `Keine Verbindung zur API (${state.apiBase}). L\u00E4uft der Server?` };
  }
  setConnected(true);
  const text = await resp.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (_e) {
    // Keine JSON-Antwort: Hinter der Adresse steht kein ProcWorks-Kern, meist
    // die Weboberflaeche selbst (sonst stuende hier „Unexpected token '<' …“).
    throw { detail: `Unter ${state.apiBase} antwortet kein ProcWorks-Server. Die API-Adresse unten in der Seitenleiste pr\u00FCfen (\u00FCblich: ${defaultApiBase()}).` };
  }
  if (!resp.ok) {
    // Demo: Sitzung verloren -- einmal still neu anmelden, statt den
    // Besucher auf einer nackten Anmeldung abzusetzen. Nicht fuer den Login
    // selbst, sonst ruft er sich endlos auf.
    if (resp.status === 401 && state.demo && state.token && path !== "/auth/login") {
      recoverDemoSession();
    }
    throw { status: resp.status, detail: data && data.detail };
  }
  return data;
}

/**
 * Antwortet unter ``base`` ein ProcWorks-Kern?
 * @param {string} base API-Basisadresse, z. B. "http://localhost/api"
 * @returns {Promise<boolean>} true bei ``GET /health`` mit JSON ``{status: "ok"}``
 */
async function isProcWorksApi(base) {
  try {
    const resp = await fetch(`${base}/health`, { cache: "no-store" });
    const data = await resp.json();
    return !!(data && data.status === "ok");
  } catch (_e) { return false; }
}

/** Laeuft gerade eine Wiederanmeldung der Demo? (nur eine zugleich) */
let demoRecovering = false;

/**
 * Stellt eine verlorene Demo-Sitzung wieder her.
 *
 * Die Demo-Instanz haelt Sitzungen und Daten im Speicher. Startet die Maschine
 * nach einer Leerlaufpause neu, ist beides weg -- der Besucher landete nach
 * ~45 min ohne Hinweis und ohne Demo-Leiste auf der Anmeldung. Hier meldet sich
 * der Client als dieselbe Demo-Person wieder an (Passwort kennt er aus
 * ``/auth/config``) und sagt ehrlich, was passiert ist. Scheitert das, bleibt
 * die normale Anmeldung.
 * @returns {Promise<boolean>} true, wenn wieder angemeldet
 */
async function recoverDemoSession() {
  if (demoRecovering || !state.demo || !state.demoPassword) return false;
  demoRecovering = true;
  try {
    const login = storageGet(sessionStorage, DEMO_LAST_LOGIN_KEY) || state.demoAutologin;
    if (!login || !(await demoLoginAs(login))) return false;
    toast("info", "Die Demo wurde nach einer Pause neu gestartet",
      ["Du bist wieder angemeldet. Was du vorher angelegt hattest, ist zur\u00FCckgesetzt."]);
    await boot();
    return true;
  } finally {
    demoRecovering = false;
  }
}

// Build request headers, attaching the bearer token when the user is logged in.
function authHeaders(hasBody) {
  const headers = {};
  if (hasBody) headers["Content-Type"] = "application/json";
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  return headers;
}

const api = {
  get: (p) => request("GET", p),
  post: (p, b) => request("POST", p, b === undefined ? {} : b),
  put: (p, b) => request("PUT", p, b === undefined ? {} : b),
  patch: (p, b) => request("PATCH", p, b === undefined ? {} : b),
  del: (p) => request("DELETE", p),
  raw: async (p) => {
    const resp = await fetch(state.apiBase + p, { headers: authHeaders(false) });
    if (!resp.ok) throw { status: resp.status, detail: "Export fehlgeschlagen" };
    return resp.text();
  },
};

function setConnected(ok) {
  const pill = byId("conn-pill");
  pill.textContent = ok ? "verbunden" : "getrennt";
  pill.className = "pill " + (ok ? "pill-green" : "pill-gray");
}

// Show the running software version (reported by the API's /health endpoint,
// which reads it from the installed package -- the single source of truth).
function showVersion(version) {
  const el = byId("app-version");
  if (!el) return;
  el.textContent = version ? `ProcWorks v${version}` : "ProcWorks";
}

// --------------------------------------------------------------------------
// Modal
// --------------------------------------------------------------------------

/**
 * Oeffnet den einen Dialog der App (``#modal-root``) und ersetzt dabei einen
 * offenen.
 *
 * @param {string} title Ueberschrift
 * @param {Node} bodyNode Inhalt
 * @param {() => (boolean|undefined|Promise<boolean|undefined>)} onConfirm
 *   Rueckruf des Bestaetigen-Knopfs. Alles ausser ``false`` schliesst danach
 *   den Dialog. **Oeffnet der Rueckruf selbst einen Folgedialog, muss er
 *   ``false`` zurueckgeben** – sonst schliesst dieser Aufruf den Container und
 *   nimmt den Folgedialog mit (so verschwanden die Zugangsdaten eines neuen
 *   Logins ungesehen). Ein Waechter prueft das fuer alle Aufrufer.
 * @param {string} [confirmLabel] Beschriftung des Bestaetigen-Knopfs
 * @param {{cancel?: boolean, danger?: boolean}} [opts]
 *   ``cancel: false`` – Ergebnisdialog: kein „Abbrechen“ (es gibt nichts
 *   abzubrechen) und kein Schliessen per Klick neben den Dialog, damit ein
 *   einmalig gezeigtes Ergebnis (Passwort) nicht versehentlich verschwindet;
 *   Escape und der eine Knopf schliessen weiterhin.
 *   Unabhaengig davon schliesst ein Klick daneben nie einen Dialog, in dem
 *   schon etwas eingegeben wurde (input/change); er zeigt dann einen Hinweis.
 *   ``danger: true`` – zerstoerende Aktion: Knopf als Gefahr gestaltet; ohne
 *   Eingabefelder startet der Fokus auf „Abbrechen“, damit Enter nichts
 *   unwiderruflich ausloest (mit Feldern im ersten Feld).
 * @returns {{modal: HTMLElement, confirmBtn: HTMLElement, close: () => void}}
 */
function openModal(title, bodyNode, onConfirm, confirmLabel, opts) {
  const o = opts || {};
  const withCancel = o.cancel !== false;
  const root = byId("modal-root");
  // Wer den Dialog oeffnete, bekommt den Fokus danach zurueck. Kommt
  // der Aufruf aus einem schon offenen Dialog (Dialog ersetzt Dialog), zaehlt
  // dessen Ausloeser, nicht der verschwindende Knopf darin.
  const opener = root.contains(document.activeElement)
    ? openModal._opener : document.activeElement;
  openModal._opener = opener;
  clear(root);
  const close = () => {
    clear(root);
    // Sperre sofort aufheben: Der Beobachter meldet sich erst asynchron, und in
    // eine noch ``inert`` gesetzte App laesst sich kein Fokus zurueckgeben.
    syncInert();
    if (opener && opener.isConnected && typeof opener.focus === "function") {
      try { opener.focus(); } catch (_e) { /* nur Komfort */ }
    }
  };
  const confirmBtn = el("button", {
    class: o.danger ? "btn danger" : "btn primary",
    onClick: async () => {
      const ok = await onConfirm();
      if (ok !== false) close();
    },
  }, confirmLabel || "Anwenden");
  const cancelBtn = withCancel ? el("button", { class: "btn ghost", onClick: close }, "Abbrechen") : null;
  // Ein Klick neben den Dialog schliesst ihn nur, solange nichts eingegeben
  // wurde. Vorher verwarf er still einen halb gestalteten Maskenentwurf; jetzt
  // bleibt der Dialog stehen und sagt, wie man bewusst verwirft (Abbrechen/Esc).
  let edited = false;
  const keepHint = el("span", { class: "modal-keep-hint", role: "status" });
  const modal = el("div", { class: "modal-backdrop",
      onClick: (e) => {
        if (!withCancel || e.target !== e.currentTarget) return;
        if (edited) {
          keepHint.textContent = "Ungespeicherte Eingaben \u2013 zum Verwerfen \u201EAbbrechen\u201C oder Esc.";
          // Der Klick hat den Fokus aus dem Dialog genommen; ohne ihn erreichte
          // Esc den Dialog nicht mehr (sein Tastenweg haengt am Dialog).
          const back = lastFocus && modal.contains(lastFocus) ? lastFocus
            : modal.querySelector("input, select, textarea, button");
          if (back && typeof back.focus === "function") back.focus();
          return;
        }
        close();
      } },
    el("div", { class: "modal" },
      el("div", { class: "modal-h" }, el("h3", null, title)),
      el("div", { class: "modal-b" }, bodyNode),
      el("div", { class: "modal-f" }, keepHint, cancelBtn, confirmBtn)));
  const markEdited = () => { edited = true; };
  modal.addEventListener("input", markEdited);
  modal.addEventListener("change", markEdited);
  let lastFocus = null;
  modal.addEventListener("focusin", (e) => { lastFocus = e.target; });
  // Enter bestaetigt den Dialog -- einmal hier fuer alle Aufrufer statt je
  // Dialog.
  // Ausnahmen: mehrzeilige Felder (Enter = Zeilenumbruch), fokussierte Knoepfe
  // und Links (Enter loest deren eigene Aktion aus), Auswahllisten, jede
  // Zusatztaste und laufende IME-Eingaben.
  modal.addEventListener("keydown", (e) => {
    // Escape schliesst wie „Abbrechen“. Der globale Escape-Handler
    // ueberlaesst die Taste bewusst dem offenen Dialog.
    if (e.key === "Escape" && !e.isComposing) { e.preventDefault(); close(); return; }
    // Tab bleibt im Dialog und laeuft im Kreis. ``inert`` sperrt nur die
    // App; Tour-Angebot, Meldungen und Demo-Leiste liegen daneben und haetten
    // den Fokus sonst aus dem Dialog gezogen.
    if (e.key === "Tab") {
      const focusables = [...modal.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
        .filter((x) => !x.disabled && x.offsetParent !== null);
      if (!focusables.length) return;
      const first = focusables[0], last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      return;
    }
    if (e.key !== "Enter" || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey || e.isComposing) return;
    const t = e.target;
    const tag = t && t.tagName;
    if (tag === "TEXTAREA" || tag === "BUTTON" || tag === "A" || tag === "SELECT") return;
    e.preventDefault();
    confirmBtn.click();
  });
  root.appendChild(modal);
  // Fokus in den Dialog: ins erste Feld, sonst auf „Bestaetigen“. Ohne
  // Feld blieb er auf dem Knopf hinter dem Dialog; Tab lief dann nur durch die
  // Seite und erreichte „Abschliessen“ nie. Die Seite dahinter ist waehrend
  // des Dialogs ``inert`` (siehe watchInert), Tab bleibt also im Dialog.
  // Zerstoerende Aktion: Fokus auf „Abbrechen“ – Enter auf einem Knopf loest
  // dessen eigene Aktion aus, also bricht Enter hier ab statt zu loeschen.
  const firstInput = modal.querySelector("input:not([readonly]), select, textarea");
  // Mit Eingabefeldern (etwa Pflicht-Anlass) beginnt man dort; ohne Felder
  // landet der Fokus auf „Abbrechen“.
  if (o.danger && cancelBtn && !firstInput) cancelBtn.focus();
  else (firstInput || confirmBtn).focus();
  // Rueckgabe fuer Dialoge, die ihren Bestaetigen-Knopf je nach Eingabe
  // sperren (z. B. der Einfuege-Dialog ohne Diskriminator). Bisherige Aufrufer
  // ignorieren sie.
  return { modal, confirmBtn, close };
}

// --------------------------------------------------------------------------
// Graph-Layout (Longest-Path-Layering, blockstrukturierter DAG)
// --------------------------------------------------------------------------

// Chip-Metrik der Knoten-Badges. Wird von renderNodeBadges (Zeichnen) UND von
// layoutSchema (Platz reservieren) verwendet, damit der reservierte vertikale
// Platz exakt zur gezeichneten Badge-Hoehe passt.
const CHIP_H = 16, CHIP_GAP = 3, CHIP_TOP = 5, CHIP_BOTTOM = 4, CHIP_PADX = 7, CHIP_CH = 6;

// Reine Chip-Modelle eines Knotens: je Datenbindung ein benannter Chip plus –
// falls vorhanden – ein Bearbeiter-Chip. Ab dem dritten Datenelement wird zu
// einem Sammel-Chip verdichtet. Von Zeichnung und Layout gemeinsam genutzt,
// damit beide dieselbe Anzahl/Reihenfolge sehen (kein Auseinanderdriften).
function nodeChipModels(schema, node) {
  if (node.type !== NODE_TYPE.ACTIVITY && node.type !== NODE_TYPE.SUBPROCESS) return [];
  const accesses = (schema.data_accesses || []).filter((a) => a.node_id === node.id);
  const rule = (schema.staff_rules || {})[node.id];
  const sym = (mode) => (mode === "WRITE" ? "✎" : mode === "READ_WRITE" ? "⇄" : "◉");
  const chips = [];
  if (accesses.length) {
    const named = accesses.slice(0, accesses.length <= 3 ? 3 : 2);
    named.forEach((a) => {
      const e = schema.data_elements[a.element_id];
      const name = e ? e.name : a.element_id;
      chips.push({
        kind: "data",
        label: sym(a.mode) + " " + truncate(name, 16),
        title: name + " (" + accessModeLabel(a.mode) + (a.mandatory ? ", Pflicht" : ", optional") + ")",
      });
    });
    const rest = accesses.length - named.length;
    if (rest > 0) {
      const detail = accesses.slice(named.length).map((a) => {
        const e = schema.data_elements[a.element_id];
        return (e ? e.name : a.element_id) + " (" + accessModeLabel(a.mode) + ")";
      }).join(", ");
      chips.push({ kind: "data", label: "+" + rest + " Daten", title: "Weitere Datenbindungen: " + detail });
    }
  }
  if (rule) chips.push({ kind: "staff", label: "Bearbeiter", title: describeRule(rule) });
  return chips;
}

// Vertikaler Platz, den der untereinander gestapelte Badge-Block eines Knotens
// unter seinem Rechteck einnimmt (0 ohne Chips). layoutSchema addiert dies auf
// die Knotenhoehe, damit Badges nie in den naechsten Knoten ragen.
function nodeBadgeStackHeight(schema, node) {
  const n = nodeChipModels(schema, node).length;
  return n ? CHIP_TOP + n * CHIP_H + (n - 1) * CHIP_GAP + CHIP_BOTTOM : 0;
}

// Geometrie-Konstanten des Kontrollfluss-Layouts (von beiden Layout-Varianten
// geteilt): Knotenbreite/-hoehe, horizontaler/vertikaler Abstand, Rand.
const LAYOUT_NW = 144, LAYOUT_NH = 56, LAYOUT_HGAP = 74, LAYOUT_VGAP = 26, LAYOUT_PAD = 32;
// Zusatzabstand zwischen zwei Geschwister-Aesten einer Verzweigung, in
// Lane-Einheiten (0 = Aeste nur um eine Bahn getrennt).
const LAYOUT_BRANCH_GAP = 0.25;

// Bevorzugtes Layout: horizontale "Spine" mit symmetrischen Aesten
// (layoutSchemaSpine). Schlaegt die Ableitung aus der Blockstruktur fehl (nicht
// wohlgeformte Kanten, Zyklus, Ueberlappung), faellt es verlustfrei auf das
// bisherige, robuste gestapelte Layout (layoutSchemaStacked) zurueck.
// Stabilitaet vor Optik: eine fehlerhafte Zeichnung darf nie entstehen.
// K4: SYNC-Kanten sind reine Warte-Beziehungen (Ziel wartet, bis die Quelle
// abgeschlossen oder abgewählt ist) und für JEDE Strukturlogik unsichtbar –
// Layout, Zweig-/Schleifenerkennung, Verschiebe-Ziele und Nachbarschaft
// arbeiten ausschließlich auf controlEdges. Gezeichnet werden Sync-Kanten
// separat (renderGraph, gestrichelt).
function controlEdges(schema) {
  return (schema.edges || []).filter((e) => e.type !== "SYNC");
}
function syncEdges(schema) {
  return (schema.edges || []).filter((e) => e.type === "SYNC");
}

function layoutSchema(schema) {
  try {
    return layoutSchemaSpine(schema);
  } catch (_e) {
    return layoutSchemaStacked(schema);
  }
}

// Spine-Layout (Nutzeranforderung): Alle Aktivitaeten liegen auf einer
// gemeinsamen horizontalen Achse; Datenobjekte/Bearbeiter haengen als Badges
// darunter. Der Hauptfluss verlaeuft moeglichst gerade horizontal von links
// nach rechts; an Verzweigungen fanen die Aeste symmetrisch nach oben/unten aus
// und laufen am zugehoerigen Join wieder auf der Achse zusammen. Das Modell ist
// block-strukturiert (garantiert durch die K-Regeln des Kerns), daher laesst
// sich die Lane (vertikale Bahn) je Knoten rekursiv aus der Blockstruktur
// ableiten:
//  * Spalte  = Laengster-Pfad-Tiefe (Fluss schreitet nach rechts fort).
//  * Lane 0  = Spine; jeder Knoten mit genau einem Vorgaenger erbt dessen Lane
//             (der Hauptpfad bleibt gerade).
//  * Split   = seine Aeste werden symmetrisch um die Split-Lane verteilt, die
//             Bandbreite je Ast aus dessen gemessener Hoehe.
//  * Join    = kehrt auf die Split-Lane (Spine) zurueck.
// Wirft bei nicht wohlgeformter Struktur -> Fallback greift.
function layoutSchemaSpine(schema) {
  const nodes = schema.nodes || {};
  const ids = Object.keys(nodes);
  if (!ids.length) return { pos: {}, edges: controlEdges(schema), width: 560, height: 160 };

  const out = {}, inc = {};
  ids.forEach((id) => { out[id] = []; inc[id] = []; });
  controlEdges(schema).forEach((e) => {
    if (out[e.source] && nodes[e.target]) out[e.source].push(e.target);
    if (inc[e.target] && nodes[e.source]) inc[e.target].push(e.source);
  });

  // Spalte = Laengster-Pfad-Tiefe (wie bisher). Ein Zyklus ist in einem
  // block-strukturierten Prozess ausgeschlossen -> wirft (Fallback).
  const depth = {}, visiting = {};
  function d(id) {
    if (depth[id] !== undefined) return depth[id];
    if (visiting[id]) throw new Error("cycle");
    visiting[id] = true;
    let m = 0;
    inc[id].forEach((p) => { m = Math.max(m, d(p) + 1); });
    visiting[id] = false;
    return (depth[id] = m);
  }
  ids.forEach(d);

  // Genau eine Wurzel (START) erwartet; sonst ist der Fluss nicht eindeutig
  // horizontal auffaedelbar -> Fallback.
  const roots = ids.filter((id) => inc[id].length === 0);
  if (roots.length !== 1) throw new Error("not single-rooted");

  // Passender Join eines Splits = gemeinsamer Nachfahre aller Split-Kinder mit
  // kleinster Tiefe (dort schliesst der Block). Memoisiert.
  const joinCache = {};
  function descendants(id) {
    const seen = new Set(), stack = [id];
    while (stack.length) {
      const n = stack.pop();
      out[n].forEach((s) => { if (!seen.has(s)) { seen.add(s); stack.push(s); } });
    }
    return seen;
  }
  function matchingJoin(splitId) {
    if (joinCache[splitId]) return joinCache[splitId];
    let common = null;
    out[splitId].forEach((k) => {
      const ds = descendants(k);
      ds.add(k); // leerer Ast: das Kind kann selbst schon der Join sein
      common = common === null ? ds : new Set([...common].filter((x) => ds.has(x)));
    });
    if (!common || !common.size) throw new Error("no matching join");
    let best = null;
    common.forEach((c) => { if (best === null || depth[c] < depth[best]) best = c; });
    return (joinCache[splitId] = best);
  }

  // Einzigen Nachfolger eines Joins liefern (oder die Sequenz-Grenze/das Ende).
  const onlySucc = (j, stop) => (j === stop ? stop : (out[j].length ? out[j][0] : null));

  // Vertikale Ausdehnung (Lane-Einheiten) der Sequenz [start .. stop): linearer
  // Knoten = 1; ein verschachtelter Split traegt die Summe seiner Ast-Hoehen.
  // Entlang der Sequenz zaehlt das Maximum (Segmente liegen horizontal
  // hintereinander, nicht uebereinander).
  function measure(start, stop) {
    let node = start, span = 1, guard = 0;
    while (node !== stop && node != null) {
      if (guard++ > ids.length + 2) throw new Error("runaway measure");
      const succ = out[node];
      if (succ.length > 1) {
        const j = matchingJoin(node);
        let block = LAYOUT_BRANCH_GAP * (succ.length - 1);
        succ.forEach((c) => { block += measure(c, j); });
        span = Math.max(span, block);
        node = onlySucc(j, stop);
      } else {
        node = succ.length === 1 ? succ[0] : null;
      }
    }
    return span;
  }

  // Sequenz [start .. stop) auf Mittellinie ``center`` platzieren; Splits fanen
  // ihre Aeste symmetrisch um ``center`` aus (Block als Ganzes zentriert).
  const lane = {}, placed = new Set(), emptyLane = {};
  function place(start, center, stop) {
    let node = start, guard = 0;
    while (node !== stop && node != null) {
      if (guard++ > ids.length + 2) throw new Error("runaway place");
      if (placed.has(node)) throw new Error("revisit");
      placed.add(node);
      lane[node] = center;
      const succ = out[node];
      if (succ.length > 1) {
        const j = matchingJoin(node);
        const heights = succ.map((c) => measure(c, j));
        const total = heights.reduce((a, b) => a + b, 0) + LAYOUT_BRANCH_GAP * (succ.length - 1);
        let cursor = center - total / 2;
        succ.forEach((c, i) => {
          // Leerer Zweig (direkte Kante Split -> Join): die reservierte Bahn
          // merken, sonst zeichnete renderGraph die Kante gerade auf der
          // Mittellinie -- quer durch den Knoten eines anderen Zweigs.
          if (c === j) emptyLane[`${node}->${j}`] = cursor + heights[i] / 2;
          place(c, cursor + heights[i] / 2, j);
          cursor += heights[i] + LAYOUT_BRANCH_GAP;
        });
        if (j === stop) { node = stop; }        // Grenz-Join gehoert dem Aufrufer
        else { lane[j] = center; placed.add(j); node = onlySucc(j, stop); }
      } else {
        node = succ.length === 1 ? succ[0] : null;
      }
    }
  }
  place(roots[0], 0, null);
  if (ids.some((id) => lane[id] === undefined)) throw new Error("unplaced nodes");

  // Lane -> Pixel. Einheitliche Bahn-Hoehe = Knotenhoehe + hoechster Badge-
  // Stapel + Abstand. Dadurch beruehrt ein oberer Knoten (Badges haengen nach
  // unten) nie den in derselben Spalte darunter liegenden Knoten, egal welche
  // Lanes benachbart sind.
  let maxBadge = 0;
  ids.forEach((id) => { maxBadge = Math.max(maxBadge, nodeBadgeStackHeight(schema, nodes[id])); });
  const rowPitch = LAYOUT_NH + maxBadge + LAYOUT_VGAP;

  // Vertikalen Ursprung so waehlen, dass der oberste Knoten PAD Abstand hat.
  // Auch die Bahnen leerer Zweige zaehlen: liegt eine ganz oben oder unten,
  // muss die Zeichenflaeche sie noch enthalten.
  let minCenter = Infinity;
  ids.forEach((id) => { minCenter = Math.min(minCenter, lane[id] * rowPitch); });
  Object.values(emptyLane).forEach((ln) => { minCenter = Math.min(minCenter, ln * rowPitch); });
  const originY = LAYOUT_PAD + LAYOUT_NH / 2 - minCenter;

  const pos = {};
  let maxBottom = 0, maxCol = 0;
  ids.forEach((id) => {
    const yc = originY + lane[id] * rowPitch;
    pos[id] = { x: LAYOUT_PAD + depth[id] * (LAYOUT_NW + LAYOUT_HGAP), y: yc - LAYOUT_NH / 2, w: LAYOUT_NW, h: LAYOUT_NH };
    maxCol = Math.max(maxCol, depth[id]);
    maxBottom = Math.max(maxBottom, pos[id].y + LAYOUT_NH + nodeBadgeStackHeight(schema, nodes[id]));
  });

  // Sicherheitsnetz: ueberlappt trotz allem ein Knotenpaar (inkl. Badge-
  // Stapel), lieber das robuste gestapelte Layout nehmen.
  if (layoutHasOverlap(pos, schema)) throw new Error("overlap");

  // Bahn-Mitte (Pixel) je leerem Zweig, Schluessel "split->join".
  const edgeLanes = {};
  Object.entries(emptyLane).forEach(([key, ln]) => {
    edgeLanes[key] = originY + ln * rowPitch;
    // Platz fuer Bedingung oben und „+“ unten an der Bahn.
    maxBottom = Math.max(maxBottom, edgeLanes[key] + LAYOUT_NH / 2);
  });
  const width = LAYOUT_PAD * 2 + (maxCol + 1) * LAYOUT_NW + maxCol * LAYOUT_HGAP;
  const height = maxBottom + LAYOUT_PAD;
  return { pos, edges: controlEdges(schema), edgeLanes,
    width: Math.max(width, 560), height: Math.max(height, 160) };
}

// Prueft, ob sich zwei Knoten-Kaesten (Rechteck inkl. darunter haengendem
// Badge-Stapel) ueberlappen. Rein defensiv fuer das Spine-Layout.
function layoutHasOverlap(pos, schema) {
  const ids = Object.keys(pos);
  const box = (id) => {
    const p = pos[id];
    return { x1: p.x, y1: p.y, x2: p.x + p.w, y2: p.y + p.h + nodeBadgeStackHeight(schema, schema.nodes[id]) };
  };
  for (let i = 0; i < ids.length; i++) {
    for (let j = i + 1; j < ids.length; j++) {
      const a = box(ids[i]), b = box(ids[j]);
      if (a.x1 < b.x2 && b.x1 < a.x2 && a.y1 < b.y2 && b.y1 < a.y2) return true;
    }
  }
  return false;
}

// Fallback-Layout: das bisherige spaltenweise gestapelte, je Spalte vertikal
// zentrierte Layout. Robust fuer beliebige (auch nicht sauber block-
// strukturierte) Kantenlagen; kommt zum Zug, wenn layoutSchemaSpine wirft.
function layoutSchemaStacked(schema) {
  const ids = Object.keys(schema.nodes);
  const inc = {}, out = {};
  ids.forEach((id) => { inc[id] = []; out[id] = []; });
  controlEdges(schema).forEach((e) => {
    if (out[e.source]) out[e.source].push(e);
    if (inc[e.target]) inc[e.target].push(e);
  });
  const depth = {}, visiting = {};
  function d(id) {
    if (depth[id] !== undefined) return depth[id];
    if (visiting[id]) return 0;
    visiting[id] = true;
    let m = 0;
    inc[id].forEach((e) => { m = Math.max(m, d(e.source) + 1); });
    visiting[id] = false;
    return (depth[id] = m);
  }
  ids.forEach(d);
  const cols = {};
  ids.forEach((id) => { (cols[depth[id]] = cols[depth[id]] || []).push(id); });
  const colKeys = Object.keys(cols).map(Number).sort((a, b) => a - b);
  const NW = LAYOUT_NW, NH = LAYOUT_NH, HGAP = LAYOUT_HGAP, VGAP = LAYOUT_VGAP, PAD = LAYOUT_PAD;
  // Effektive Hoehe je Knoten = Rechteck + darunter gestapelter Badge-Block.
  // Dadurch wird eine Spalte bei Bedarf auseinandergezogen, sodass die
  // Datenbindungs-/Bearbeiter-Chips nie den naechsten Knoten ueberlagern.
  const effH = {};
  ids.forEach((id) => { effH[id] = NH + nodeBadgeStackHeight(schema, schema.nodes[id]); });
  const colTotal = {};
  colKeys.forEach((c) => {
    const list = cols[c];
    colTotal[c] = list.reduce((s, id) => s + effH[id], 0) + VGAP * Math.max(0, list.length - 1);
  });
  const height = PAD * 2 + Math.max(NH, ...colKeys.map((c) => colTotal[c]));
  const pos = {};
  colKeys.forEach((c, ci) => {
    const list = cols[c];
    let y = PAD + (height - PAD * 2 - colTotal[c]) / 2;
    list.forEach((id) => {
      pos[id] = { x: PAD + ci * (NW + HGAP), y, w: NW, h: NH };
      y += effH[id] + VGAP;
    });
  });
  const width = PAD * 2 + colKeys.length * NW + (colKeys.length - 1) * HGAP;
  return { pos, edges: controlEdges(schema), width: Math.max(width, 560), height: Math.max(height, 160) };
}

function nodeClass(node, instance) {
  if (instance && instance.node_states && instance.node_states[node.id]) {
    return "gnode s-" + instance.node_states[node.id];
  }
  if (node.type === NODE_TYPE.START || node.type === NODE_TYPE.END) return "gnode nstart";
  if (node.type === NODE_TYPE.SUBPROCESS) return "gnode nsub";
  if (GATEWAYS.has(node.type)) return "gnode ngateway";
  return "gnode ndefault";
}

/** Benennung unbenannter Verzweigungsknoten (fuer nodeCaptionInContext). */
const GATEWAY_KIND_NAMES = {
  XOR_SPLIT: "Entscheidung", XOR_JOIN: "Ende der Entscheidung",
  AND_SPLIT: "Parallele Zweige", AND_JOIN: "Ende der parallelen Zweige",
};

/**
 * Beschriftung eines Knotens mit Zusammenhang, wo die Kurzform nicht
 * unterscheidet: Mehrere unbenannte Verzweigungen hiessen alle „XOR ▶“ bzw.
 * „▶ UND“ (Ad-hoc-Auswahl, Simulation). Hier heissen sie nach dem Schritt
 * davor: „Entscheidung nach „Betrag erfassen““. Benannte Knoten bleiben, wie
 * sie sind.
 * @param {object} schema Schema
 * @param {object} node Knoten
 * @returns {string}
 */
function nodeCaptionInContext(schema, node) {
  const unnamedStep = node && !node.label && (node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS);
  if (!node || node.label || (!GATEWAYS.has(node.type) && !unnamedStep)) return node ? nodeCaption(node) : "";
  const predsOf = (id) => controlEdges(schema).filter((e) => e.target === id).map((e) => schema.nodes[e.source]);
  // Nach dem naechsten benannten Vorgaenger benennen und die unbenannten
  // Knoten dazwischen zaehlen („2. Schritt ohne Bezeichnung nach …“) -- ohne
  // Rekursion, sonst verschachtelten sich Ketten unbenannter Knoten.
  const kindKey = (x) => (x.type === NODE_TYPE.SUBPROCESS ? NODE_TYPE.ACTIVITY : x.type);
  let steps = 1, before = null, cur = node;
  for (let guard = 0; guard < 200; guard++) {
    const preds = predsOf(cur.id).filter(Boolean);
    if (!preds.length) break;
    const named = preds.find((p) => p.label);
    if (named) { before = nodeCaption(named); break; }
    if (preds.length > 1) {
      // Mehrere unbenannte Vorgaenger: Steht der Lauf schon an einem anderen
      // Knoten (Ende einer Verzweigung), ist dieser der Bezug; am Ausgangsknoten
      // selbst gibt es keinen („Ende der Entscheidung“ ohne „nach …“).
      if (cur !== node) before = GATEWAY_KIND_NAMES[cur.type] || nodeCaption(cur);
      break;
    }
    const p = preds[0];
    if (p.type === NODE_TYPE.START) { before = nodeCaption(p); break; }
    // Gleiche Bezeichnung = gleiche Zaehlung: Aktivitaet und Teilprozess
    // heissen beide „Schritt ohne Bezeichnung“.
    if (kindKey(p) === kindKey(node)) steps++;
    cur = p;
  }
  const kind = (steps > 1 ? `${steps}. ` : "")
    + (unnamedStep ? "Schritt ohne Bezeichnung" : GATEWAY_KIND_NAMES[node.type] || nodeCaption(node));
  return before ? `${kind} nach \u201E${before}\u201C` : kind;
}

/**
 * Beschriftung eines Zweigs (Kante Split -> erster Knoten): der erste Schritt,
 * bei einem leeren Zweig (Kante direkt zum Join) „leerer Zweig“ -- jeweils mit
 * Bedingung, falls vorhanden.
 * @param {object} schema Schema
 * @param {string} splitId Split
 * @param {string} targetId erster Knoten bzw. Join
 * @returns {string}
 */
function branchCaption(schema, splitId, targetId) {
  const edge = controlEdges(schema).find((e) => e.source === splitId && e.target === targetId);
  const target = schema.nodes[targetId];
  const empty = target && (target.type === NODE_TYPE.XOR_JOIN || target.type === NODE_TYPE.AND_JOIN);
  const name = empty ? "leerer Zweig" : nodeCaption(target || { type: "", label: targetId });
  const cond = edge && edge.condition ? conditionCaption(edge.condition) : "";
  return cond ? `${name} (${cond})` : name;
}

function nodeCaption(node) {
  if (node.label) return node.label;
  return { START: "Start", END: "Ende", AND_SPLIT: "UND \u25B6", AND_JOIN: "\u25B6 UND",
    XOR_SPLIT: "XOR \u25B6", XOR_JOIN: "\u25B6 XOR", SUBPROCESS: "Teilprozess",
    LOOP_START: "\u21BB Wiederholen", LOOP_END: "Bis erf\u00FCllt \u21BB" }[node.type] || nodeTypeLabel(node.type);
}

// Berechnet die Datenherkunft-Linien (Schreib- -> Lese-Knoten) fuer die
// gestrichelte Ueberlagerung im Kontrollfluss. ``focus`` steuert den Umfang:
//  * ``dataElemFocus`` gesetzt -> alle Lesestellen genau dieses Datenelements
//    werden mit ihren Schreibquellen verbunden (Palette-Klick).
//  * sonst ``selectedNode`` gesetzt -> es werden nur die vom gewaehlten Knoten
//    *gelesenen* Groessen zu ihren Schreibquellen aufgeloest.
// Rueckgabe: Liste eindeutiger {from, to, label}. Rein lesend; veraendert nie
// Modell oder Backend (die Korrektheit von D1 stellt der Kern sicher).
function computeProvenance(schema, focus) {
  focus = focus || {};
  const accesses = schema.data_accesses || [];
  const isRead = (m) => m === "READ" || m === "READ_WRITE";
  const isWrite = (m) => m === "WRITE" || m === "READ_WRITE";
  // Zu erklaerende (Element, Lese-Knoten)-Paare bestimmen.
  let reads = [];
  if (focus.dataElemFocus) {
    reads = accesses
      .filter((a) => a.element_id === focus.dataElemFocus && isRead(a.mode))
      .map((a) => ({ element_id: a.element_id, node_id: a.node_id }));
  } else if (focus.selectedNode) {
    reads = accesses
      .filter((a) => a.node_id === focus.selectedNode && isRead(a.mode))
      .map((a) => ({ element_id: a.element_id, node_id: a.node_id }));
  }
  const seen = new Set();
  const lines = [];
  reads.forEach((r) => {
    const elem = schema.data_elements[r.element_id];
    const label = elem ? elem.name : r.element_id;
    accesses
      .filter((w) => w.element_id === r.element_id && isWrite(w.mode) && w.node_id !== r.node_id)
      .forEach((w) => {
        const key = `${w.node_id}->${r.node_id}:${r.element_id}`;
        if (seen.has(key)) return;
        seen.add(key);
        lines.push({ from: w.node_id, to: r.node_id, label: truncate(label, 14) });
      });
  });
  return lines;
}

// Paare (LOOP_START -> LOOP_END) samt Rumpfmenge, per Vorwärtslauf mit
// Tiefenzählung – der Spiegel von model.loop_block im Kern (K6a garantiert die
// saubere Paarung; ein unpaariger Start kann gar nicht erst entstehen). Rein
// lesend, wird für den gezeichneten Rücksprung-Bogen gebraucht, denn die
// Rücksprungkante existiert bewusst nicht als Datum.
function loopPairsOf(schema) {
  const out = {};
  controlEdges(schema).forEach((e) => { (out[e.source] = out[e.source] || []).push(e.target); });
  const pairs = [];
  Object.values(schema.nodes || {}).forEach((n) => {
    if (n.type !== NODE_TYPE.LOOP_START) return;
    const body = new Set();
    const stack = (out[n.id] || []).map((t) => [t, 0]);
    let end = null;
    while (stack.length) {
      const [id, depth] = stack.pop();
      const node = schema.nodes[id];
      if (!node || body.has(id)) continue;
      if (node.type === NODE_TYPE.LOOP_END && depth === 0) { end = id; continue; }
      body.add(id);
      let d = depth;
      if (node.type === NODE_TYPE.LOOP_START) d += 1;
      else if (node.type === NODE_TYPE.LOOP_END) d -= 1;
      (out[id] || []).forEach((t) => stack.push([t, d]));
    }
    if (end) pairs.push({ start: n.id, end, body });
  });
  return pairs;
}

// Deutsche Kurzbeschreibung der Wiederhol-Bedingung einer LoopDecision –
// die EINE geteilte Quelle für den Rücksprung-Bogen (renderGraph) und das
// Schleifen-Panel (loopNodePanel), damit die Oberflächen nie driften.
// Boolesche Kurzform (S1): „X“ = wahr/falsch. Partition (S3): die
// Wiederhol-Zellen werden als kompakte Wertbereiche aufgezählt (THRESHOLD als
// [untere–obere) Bereiche, ENUM als Wertemenge bzw. „sonst“).
function loopConditionCaption(schema, d, maxLen) {
  if (!d) return null;
  const elem = (schema.data_elements || {})[d.discriminator];
  const name = truncate(elem ? elem.name : d.discriminator, maxLen || 14);
  if (!d.cells || !d.cells.length) {
    return `„${name}“ = ${d.repeat_value ? "wahr" : "falsch"}`;
  }
  if (d.kind === "THRESHOLD") {
    const parts = [];
    let lower = null;
    d.cells.forEach((c) => {
      if (c.repeat) {
        if (lower === null) parts.push(`< ${c.upper}`);
        else if (c.upper == null) parts.push(`≥ ${lower}`);
        else parts.push(`${lower} – ${c.upper}`);
      }
      lower = c.upper;
    });
    return `„${name}“ ${parts.join(" oder ")}`;
  }
  if (d.kind === "BOOLEAN") {
    const rep = d.cells.filter((c) => c.repeat).map((c) => (c.bool_value ? "wahr" : "falsch"));
    return `„${name}“ = ${rep.join("/")}`;
  }
  // ENUM: aufgezählte Wiederhol-Werte; wiederholt der Sonst-Zweig, steht das dabei.
  const rep = d.cells.filter((c) => c.repeat);
  const named = rep.filter((c) => !c.is_else).flatMap((c) => c.values || []);
  const bits = [];
  if (named.length) bits.push(`∈ {${named.map((v) => truncate(v, 10)).join(", ")}}`);
  if (rep.some((c) => c.is_else)) bits.push("sonst");
  return `„${name}“ ${bits.join(" oder ")}`;
}

function renderGraph(schema, opts) {
  opts = opts || {};
  const L = layoutSchema(schema);
  const root = svg("svg", { class: "graph", width: L.width, height: L.height, viewBox: `0 0 ${L.width} ${L.height}` });
  const defs = svg("defs", null,
    svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
      svg("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#6b7794" })),
    // Eigener (bernsteinfarbener) Pfeilkopf fuer die gestrichelten
    // Datenherkunft-Linien (Schreib- -> Lese-Knoten), damit sie sich klar vom
    // Kontrollfluss abheben.
    svg("marker", { id: "arrow-prov", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
      svg("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#e0a03a" })));
  root.appendChild(defs);

  // Kanten
  L.edges.forEach((e) => {
    const a = L.pos[e.source], b = L.pos[e.target];
    if (!a || !b) return;
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    const mx = (x1 + x2) / 2;
    // Leerer Zweig: ueber seine eigene Bahn fuehren (aus dem Layout), mit
    // Bedingung und „+“ auf der Bahn statt in einem fremden Knoten.
    const laneY = L.edgeLanes && L.edgeLanes[`${e.source}->${e.target}`];
    const emptyBranch = laneY !== undefined && Math.abs(laneY - y1) > 1;
    const ly = emptyBranch ? laneY : (y1 + y2) / 2;
    let cls = "gedge";
    if (opts.instance && opts.instance.edge_states) {
      const st = opts.instance.edge_states[`${e.source}->${e.target}`];
      if (st === "TRUE_SIGNALED") cls += " gedge-true";
      else if (st === "FALSE_SIGNALED") cls += " gedge-false";
    }
    const k = Math.min(40, (x2 - x1) / 4);
    const d = emptyBranch
      ? `M ${x1} ${y1} C ${x1 + k} ${y1}, ${x1 + k} ${ly}, ${x1 + 2 * k} ${ly} L ${x2 - 2 * k} ${ly} C ${x2 - k} ${ly}, ${x2 - k} ${y2}, ${x2} ${y2}`
      : `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
    root.appendChild(svg("path", { class: cls, "marker-end": "url(#arrow)", d,
      "data-edge": `${e.source}->${e.target}` }));
    if (e.condition || emptyBranch) {
      const caption = (emptyBranch ? "leerer Zweig" : "")
        + (e.condition ? (emptyBranch ? ": " : "") + conditionCaption(e.condition) : "");
      // Volle Beschriftung, solange sie keinen Knoten (samt Badges) schneidet
      // -- bei schraegen Zweigkanten liegt sie zwischen den Bahnen im Freien.
      // Sonst in die Luecke zwischen den Knoten einpassen: bis zu zwei Zeilen,
      // danach gekuerzt; der volle Text steht im Tooltip.
      const fullW = caption.length * 5.5;
      const fullBox = { x0: mx - fullW / 2, x1: mx + fullW / 2, y0: ly - 16, y1: ly - 4 };
      const lines = labelHitsNode(L, schema, fullBox) ? fitCaption(caption, x2 - x1 - 8) : [caption];
      const t = svg("text", { class: "gcond", x: mx, y: ly - 6 - (lines.length - 1) * 11, "text-anchor": "middle" });
      lines.forEach((line, i) => t.appendChild(
        svg("tspan", { x: mx, dy: i ? 11 : 0 }, document.createTextNode(line))));
      if (lines.join(" ") !== caption) t.appendChild(svg("title", null, document.createTextNode(caption)));
      root.appendChild(t);
    }
    if (opts.onPlus) {
      // data-tour/-src: Anker der gefuehrten Tour. Sie zeigt gezielt auf das
      // "+" EINER bestimmten Kante, deshalb reist die Quellknoten-Id mit; das
      // Ziel braucht der Einfuegedialog, wenn die Quelle mehrere Ausgaenge hat
      // (Anfang eines Zweigs).
      const g = svg("g", { class: "gplus-wrap", style: "cursor:pointer",
        "data-tour": "model.plus", "data-tour-src": e.source,
        onClick: () => opts.onPlus(e.source, e.target) });
      g.appendChild(svg("circle", { class: "gplus", cx: mx, cy: ly + 10, r: 10 }));
      g.appendChild(svg("text", { class: "gplus-txt", x: mx, y: ly + 14, "text-anchor": "middle" }, document.createTextNode("+")));
      root.appendChild(g);
    }
  });

  // Rücksprung-Bögen der Schleifen (K6): Die Rücksprungkante ist bewusst nie
  // gespeichert (der Graph bleibt azyklisch) – gezeichnet wird sie aus der
  // LOOP_START/LOOP_END-Paarung: vom Unterrand des Schleifenendes unter dem
  // Rumpf hindurch zurück an den Unterrand des Schleifenanfangs. Der Bogen
  // taucht unter die tiefste Bahn des Blocks (dort hängen auch die Badges);
  // ragt er unter die viewBox hinaus, wird sie nach unten erweitert –
  // das Spiegelbild der Herkunfts-Erweiterung nach oben (s. u.).
  // K4: Sync-Kanten gestrichelt zwischen den Zweigen zeichnen (Warte-
  // Beziehung; deutlich unterschieden von Kontrollfluss und Datenherkunft).
  syncEdges(schema).forEach((e) => {
    const ps = L.pos[e.source], pt = L.pos[e.target];
    if (!ps || !pt) return;
    const down = pt.y >= ps.y + ps.h;
    const x1 = ps.x + ps.w / 2, y1 = down ? ps.y + ps.h : ps.y;
    const x2 = pt.x + pt.w / 2, y2 = down ? pt.y : pt.y + pt.h;
    const ym = (y1 + y2) / 2;
    root.appendChild(svg("path", { class: "gsyncedge", "marker-end": "url(#arrow)",
      d: `M ${x1} ${y1} C ${x1} ${ym}, ${x2} ${ym}, ${x2} ${y2}` }));
  });

  let vbBottom = L.height;
  loopPairsOf(schema).forEach(({ start, end, body }) => {
    const ps = L.pos[start], pe = L.pos[end];
    if (!ps || !pe) return;
    // Unterkante inklusive der Badge-Stapel unter den Knoten: sonst lag die
    // Bogenbeschriftung („↻ solange …“) auf Daten-/Bearbeiter-Badges.
    const bottomOf = (id, p) => p.y + p.h + nodeBadgeStackHeight(schema, schema.nodes[id]);
    let low = bottomOf(start, ps);
    body.forEach((id) => { const p = L.pos[id]; if (p) low = Math.max(low, bottomOf(id, p)); });
    low = Math.max(low, bottomOf(end, pe));
    const dip = low + 30;
    const xe = pe.x + pe.w / 2, ye = pe.y + pe.h;
    const xs = ps.x + ps.w / 2, ys = ps.y + ps.h;
    root.appendChild(svg("path", { class: "gloop", "marker-end": "url(#arrow)",
      d: `M ${xe} ${ye} C ${xe} ${dip}, ${xs} ${dip}, ${xs} ${ys + 4}` }));
    const d = (schema.loop_decisions || {})[end];
    const condition = loopConditionCaption(schema, d, 14);
    const maxSuffix = d && d.max_iterations ? ` · max ${d.max_iterations}×` : "";
    const caption = condition ? `↻ solange ${condition}${maxSuffix}` : "↻ wiederholen";
    root.appendChild(svg("text", { class: "gloop-txt", x: (xs + xe) / 2, y: dip - 5, "text-anchor": "middle" },
      document.createTextNode(caption)));
    vbBottom = Math.max(vbBottom, dip + 12);
  });
  if (vbBottom > L.height) {
    root.setAttribute("height", vbBottom);
    root.setAttribute("viewBox", `0 0 ${L.width} ${vbBottom}`);
  }

  // Datenherkunft (gestrichelt): fuer jede gelesene Groesse ein Bogen vom
  // Schreib- zum Lese-Knoten. Rein visuell; ``opts.provenance`` wird von der
  // Modellieren-Sicht aus ``computeProvenance`` gefuellt. Ein leicht nach oben
  // versetzter Bogen vermeidet Deckung mit den Kontrollflusskanten.
  //
  // Zwei Sichtbarkeitsprobleme werden hier bewusst behandelt, damit die
  // Beschriftung (Name des Datenelements) *immer* lesbar bleibt:
  //  1. Der Bogen hebt sich um bis zu ~94px ueber die oberste Knotenzeile
  //     (die schon bei y=PAD sitzt) und ragt damit oberhalb der viewBox
  //     (y=0) aus dem sichtbaren Bereich. Wir bestimmen den obersten
  //     erreichten Punkt und erweitern die viewBox bei Bedarf nach oben.
  //  2. Enden mehrere Boegen am selben Leseknoten (mehrere gelesene Groessen),
  //     liegen ihre Beschriftungen fast uebereinander. Wir staffeln
  //     kollidierende Labels vertikal nach oben.
  const provItems = (opts.provenance || []).map((pv) => {
    const a = L.pos[pv.from], b = L.pos[pv.to];
    if (!a || !b) return null;
    const x1 = a.x + a.w / 2, y1 = a.y, x2 = b.x + b.w / 2, y2 = b.y;
    const lift = 34 + Math.min(60, Math.abs(x2 - x1) * 0.12);
    const my = Math.min(y1, y2) - lift;
    // ``a``/``b`` reisen mit, damit unten der Bereich bestimmt werden kann, den
    // die Herkunft insgesamt einnimmt (Bogen *und* beide beteiligten Knoten).
    return { x1, y1, x2, y2, my, from: a, to: b,
      label: pv.label, lx: (x1 + x2) / 2, ly: my + 4 };
  }).filter(Boolean);

  // Labels vertikal entzerren: nach Bogenspitze (oben zuerst) sortieren und
  // jedes Label so weit nach oben schieben, dass es kein bereits platziertes
  // Label in der Naehe (gleicher horizontaler Bereich) mehr ueberdeckt.
  const LBL_LINE = 12; // Zeilenhoehe der 9px-Beschriftung inkl. kleinem Rand
  const LBL_HALF_W = 45; // grobe halbe Breite einer (auf 14 Zeichen gekuerzten) Beschriftung
  const placed = [];
  provItems.slice().sort((p, q) => p.ly - q.ly).forEach((pv) => {
    if (!pv.label) return;
    let guard = 0;
    let hit = true;
    while (hit && guard++ < 50) {
      hit = false;
      for (const q of placed) {
        if (Math.abs(q.lx - pv.lx) < 64 && Math.abs(q.ly - pv.ly) < LBL_LINE) {
          pv.ly = q.ly - LBL_LINE; hit = true; break;
        }
      }
    }
    placed.push(pv);
  });

  // viewBox nach oben erweitern, falls Bogen oder Label ueber y=0 hinausragen
  // (Bogenscheitel ~pv.my, Labeloberkante ~pv.ly-9). So wird nichts mehr
  // abgeschnitten; die Modellsicht waechst nur nach oben, Knoten bleiben fix.
  let topY = 0;
  provItems.forEach((pv) => { topY = Math.min(topY, pv.my - 2, pv.ly - 10); });
  // Der Schnellring des gewaehlten Knotens (siehe renderNodeRing) sitzt UEBER
  // dem Knotenrechteck. Bei einem Knoten auf der obersten Bahn (y = LAYOUT_PAD)
  // ragt er sonst ueber den Rand der viewBox hinaus und waere unklickbar --
  // deshalb geht er in dieselbe Erweiterung nach oben ein wie die Herkunft.
  const ringPos = opts.onNodeAction && opts.selectedId ? L.pos[opts.selectedId] : null;
  if (ringPos && nodeRingActions(schema, schema.nodes[opts.selectedId]).length) {
    topY = Math.min(topY, ringPos.y - RING_LIFT - RING_R - 2);
  }
  if (topY < 0) {
    const vbTop = topY - 6;
    root.setAttribute("viewBox", `0 ${vbTop} ${L.width} ${vbBottom - vbTop}`);
    root.setAttribute("height", vbBottom - vbTop);
  }

  provItems.forEach((pv) => {
    root.appendChild(svg("path", {
      class: "gprov", "marker-end": "url(#arrow-prov)",
      d: `M ${pv.x1} ${pv.y1} C ${pv.x1} ${pv.my}, ${pv.x2} ${pv.my}, ${pv.x2} ${pv.y2}` }));
    if (pv.label) {
      root.appendChild(svg("text", { class: "gprov-txt", x: pv.lx, y: pv.ly, "text-anchor": "middle" },
        document.createTextNode(pv.label)));
    }
  });

  // Umschliessender Bereich der Datenherkunft (Bogen + Beschriftung + **beide**
  // beteiligten Knoten). Er wandert an die Canvas, damit das Einrasten auf den
  // gewaehlten Knoten die Herkunft nicht aus dem Bild schiebt: Der Schreiber
  // liegt typischerweise mehrere hundert Pixel weiter links, sodass ein reines
  // Zentrieren auf den Leseknoten genau das versteckte, was der Bogen zeigen
  // soll -- die Verbindung zwischen den beiden Schritten (siehe centerOn).
  const provBounds = provItems.length ? provItems.reduce((acc, pv) => {
    const parts = [
      [pv.from.x, pv.from.y, pv.from.w, pv.from.h],
      [pv.to.x, pv.to.y, pv.to.w, pv.to.h],
      [pv.lx - LBL_HALF_W, Math.min(pv.my, pv.ly - LBL_LINE), LBL_HALF_W * 2, LBL_LINE],
    ];
    parts.forEach(([x, y, w, h]) => {
      acc.x0 = Math.min(acc.x0, x); acc.y0 = Math.min(acc.y0, y);
      acc.x1 = Math.max(acc.x1, x + w); acc.y1 = Math.max(acc.y1, y + h);
    });
    return acc;
  }, { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity }) : null;

  // Knoten
  Object.entries(L.pos).forEach(([id, p]) => {
    const node = schema.nodes[id];
    let cls = nodeClass(node, opts.instance);
    if (opts.selectedId === id) cls += " selected";
    // data-node-id: adressierbare Knoten-Gruppe (z. B. für die
    // Simulations-Abspielanimation, die Schritte nacheinander hervorhebt).
    const g = svg("g", { class: cls, "data-node-id": id,
      style: opts.onSelectNode ? "cursor:pointer" : "",
      onClick: opts.onSelectNode ? () => opts.onSelectNode(id) : null });
    g.appendChild(svg("rect", { x: p.x, y: p.y, width: p.w, height: p.h, rx: 10 }));
    // Gekuerzte Bezeichnung: die volle steht als Tooltip am Knoten.
    const caption = nodeCaption(node);
    if (caption.length > 18) g.appendChild(svg("title", null, document.createTextNode(caption)));
    g.appendChild(svg("text", { class: "glabel", x: p.x + p.w / 2, y: p.y + p.h / 2 - 2, "text-anchor": "middle" },
      document.createTextNode(truncate(caption, 18))));
    // E2-Status-Overlay (Stufe C): ein angehaltener
    // oder gescheiterter Schritt ist direkt in der Prozesslandkarte sichtbar,
    // nicht erst in der Aufgabenliste -- Statustext + Randfarbe am Knoten.
    const detail = opts.instance && opts.instance.node_details
      ? opts.instance.node_details[id] : null;
    let sub = opts.instance && opts.instance.node_states
      ? nodeStateLabel(opts.instance.node_states[id] || "") : nodeTypeLabel(node.type);
    // Soll-Ist-Sicht (opts.observed, aus /schemas/{id}/conformance): Haeufigkeit
    // und mittlere Dauer am Schritt; nie ausgefuehrte Schritte blass.
    const obs = opts.observed && opts.observed[id];
    if (obs) {
      sub = obs.completed ? `${obs.completed}× · Ø ${fmtStepDuration(obs.avg_total_seconds)}` : "nie ausgeführt";
      if (!obs.completed) g.classList.add("obs-none");
    }
    if (detail === "SUSPENDED") { sub += " · angehalten"; g.classList.add("d-suspended"); }
    else if (detail === "FAILED") { sub += " · gescheitert"; g.classList.add("d-failed"); }
    g.appendChild(svg("text", { class: "gstate", x: p.x + p.w / 2, y: p.y + p.h / 2 + 14, "text-anchor": "middle" },
      document.createTextNode(sub)));
    // Iterationszähler (K6, rein beobachtend): Wie oft hat diese Schleife
    // bereits wiederholt? Nur in Laufzeit-Sichten (Instanz vorhanden) und nur,
    // wenn mindestens einmal wiederholt wurde.
    const iters = opts.instance && opts.instance.loop_iterations
      ? opts.instance.loop_iterations[id] : null;
    if (node.type === NODE_TYPE.LOOP_END && iters) {
      g.appendChild(svg("text", { class: "gloop-iter", x: p.x + p.w / 2, y: p.y + p.h + 14, "text-anchor": "middle" },
        document.createTextNode(`↻ ${iters}× wiederholt`)));
    }
    root.appendChild(g);
    renderNodeBadges(root, schema, node, p, opts);
    renderNodeFindingMark(root, node, p, opts);
    renderNodeRing(root, schema, node, p, opts);
  });

  // Einpassen-Knopf oben rechts **im** Canvas (nicht im Panel-Kopf): so steht er
  // in jeder Sicht zur Verfuegung, die einen Kontrollfluss zeichnet -- auch in
  // Ausfuehrung/Monitoring, wo es keinen Panel-Kopf mit Knoepfen gibt. Er holt
  // ein verschobenes/gezoomtes Modell wieder vollstaendig ins Bild; derselbe
  // Weg wie der Doppelklick mit der mittleren Maustaste (siehe attachPanZoom).
  const fitBtn = el("button", {
    class: "canvas-fit",
    type: "button",
    title: "Modell einpassen – erster Klick: lesbar ab Start, zweiter Klick: ganze Übersicht (auch: Doppelklick mit der mittleren Maustaste)",
    "aria-label": "Modell in die Ansicht einpassen",
    onClick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (wrap._panzoom) wrap._panzoom.fitToView();
    },
  }, "\u2922 Einpassen");
  const wrap = el("div", { class: "canvas-wrap", "data-tour": "model.graph" }, root, fitBtn,
    el("div", { class: "canvas-hint" }, "Scrollen/Wischen: Verschieben \u00B7 Strg/Pinch: Zoom \u00B7 Ziehen: Verschieben"));
  // Der Herkunfts-Bereich haengt an der Canvas, weil nur sie ihre eigene Groesse
  // kennt (das Einpassen passiert erst nach dem Einhaengen ins Dokument).
  wrap._provBounds = provBounds;
  attachPanZoom(wrap, root);
  if (opts.fitOnShow) fitWhenVisible(wrap);
  else if (opts.instance) fitWhenVisible(wrap, activeRegion(L, opts.instance));
  return wrap;
}

/**
 * Schneidet ein Beschriftungsrechteck (Modellkoordinaten) einen Knoten samt
 * darunter haengendem Badge-Stapel?
 * @param {{pos: Object<string, {x:number,y:number,w:number,h:number}>}} L Layout
 * @param {object} schema Schema (fuer die Badge-Hoehe)
 * @param {{x0:number,x1:number,y0:number,y1:number}} box Rechteck
 * @returns {boolean}
 */
function labelHitsNode(L, schema, box) {
  return Object.entries(L.pos).some(([id, p]) => {
    const bottom = p.y + p.h + nodeBadgeStackHeight(schema, (schema.nodes || {})[id] || {});
    return box.x0 < p.x + p.w && box.x1 > p.x && box.y0 < bottom && box.y1 > p.y;
  });
}

/**
 * Bricht eine Kantenbeschriftung auf hoechstens zwei Zeilen um, die in eine
 * Breite in Pixeln passen (Schaetzung ~5,5 px je Zeichen bei 10 px Schrift);
 * was dann noch uebersteht, endet mit „…“. Umbrochen wird an Leerzeichen.
 * @param {string} text Beschriftung
 * @param {number} px verfuegbare Breite
 * @returns {string[]} eine oder zwei Zeilen (eine, wenn sie passt)
 */
function fitCaption(text, px) {
  const max = Math.max(4, Math.floor(px / 5.5));
  if (text.length <= max) return [text];
  const words = text.split(" ");
  const lines = [""];
  for (const w of words) {
    const cur = lines[lines.length - 1];
    if (!cur || (cur + " " + w).length <= max) lines[lines.length - 1] = cur ? cur + " " + w : w;
    else lines.push(w);
  }
  const cut = (l) => (l.length <= max ? l : l.slice(0, max - 1) + "\u2026");
  if (lines.length <= 2) return lines.map(cut);
  return [cut(lines[0]), cut(lines.slice(1).join(" "))];
}

/**
 * Bereich (Modellkoordinaten) um die gerade aktiven Schritte eines Vorgangs.
 *
 * Eine Live-Landkarte startete oben links im Bild; bei einem grossen Prozess
 * (Order-to-Cash) lag dort nichts, und man sah eine leere Flaeche, bis man
 * „Einpassen“ drueckte. Jetzt rueckt die Karte die bereiten bzw. laufenden
 * Schritte ins Bild -- auch nach jedem Neuaufbau.
 *
 * @param {{pos: Object<string, {x:number,y:number,w:number,h:number}>}} L Layout
 * @param {{node_states?: Object<string, string>}} instance Vorgang bzw. Simulation
 * @returns {{x0:number,y0:number,x1:number,y1:number}|null} Bereich, oder null
 *   ohne aktiven Schritt (dann wird das ganze Modell eingepasst)
 */
function activeRegion(L, instance) {
  const states = (instance && instance.node_states) || {};
  const ids = Object.keys(L.pos).filter((id) => states[id] === "ACTIVATED" || states[id] === "RUNNING");
  if (!ids.length) return null;
  const r = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  ids.forEach((id) => {
    const p = L.pos[id];
    r.x0 = Math.min(r.x0, p.x); r.y0 = Math.min(r.y0, p.y);
    r.x1 = Math.max(r.x1, p.x + p.w); r.y1 = Math.max(r.y1, p.y + p.h);
  });
  return r;
}

/** Hoechstzahl Bilder, die fitWhenVisible auf das Einhaengen wartet (~3 s). */
const FIT_WAIT_FRAMES = 180;

/**
 * Passt eine Zeichenflaeche ein, sobald sie im Dokument haengt und Groesse hat.
 *
 * ``fitToView`` braucht die tatsaechliche Groesse des Rahmens -- die gibt es
 * erst nach dem Einhaengen. Eine Sicht, die ihren Graphen loesgeloest baut und
 * erst danach anhaengt (Soll/Ist-Karte im Monitoring), stand deshalb beim
 * ersten Oeffnen leer da: das Modell lag ausserhalb des Ausschnitts, und man
 * musste selbst "Einpassen" druecken.
 *
 * Wartet hoechstens etwa drei Sekunden (180 Bilder) -- danach ist die Flaeche
 * entweder da oder die Sicht wurde laengst wieder verlassen; ein ewiger
 * Ruecksprung waere ein Leck. Eine halbe Sekunde reichte nicht: Die
 * Vorgangsansicht haengt ihre Karte erst nach weiteren API-Aufrufen ein (Audit,
 * Aufgaben), und die Landkarte blieb dann doch oben links stehen.
 *
 * @param {HTMLElement} wrap die Zeichenflaeche aus renderGraph
 * @param {{x0:number,y0:number,x1:number,y1:number}|null} [focus] statt ganz
 *   einzupassen diesen Bereich ins Bild ruecken (aktive Schritte eines Vorgangs)
 */
function fitWhenVisible(wrap, focus) {
  let tries = 0;
  const attempt = () => {
    if (!wrap.isConnected || !wrap.clientWidth || !wrap.clientHeight) {
      if (++tries < FIT_WAIT_FRAMES) requestAnimationFrame(attempt);
      return;
    }
    if (!wrap._panzoom) return;
    if (focus) wrap._panzoom.centerOn(null, focus);
    else wrap._panzoom.fitToView();
  };
  requestAnimationFrame(attempt);
}

// Zeichnet die Knoten-Badges (Datenbindungen + Bearbeiter) als **vertikalen
// Stapel** UNTER dem Knoten. Jeder Chip ist auf die Knotenbreite gedeckelt und
// zentriert, sodass er nie seitlich in einen Nachbarknoten ragt; den vertikalen
// Platz reserviert layoutSchema ueber nodeBadgeStackHeight. In der Modellieren-
// Sicht sind die Chips klickbar (Sprung in die Daten-/Ressourcensicht), sonst
// rein informativ. Rein visuell -- veraendert nie Modell oder Backend.
function renderNodeBadges(root, schema, node, p, opts) {
  const chips = nodeChipModels(schema, node);
  if (!chips.length) return;
  let y = p.y + p.h + CHIP_TOP;
  chips.forEach((chip) => {
    const onOpen = chip.kind === "data" ? opts.onOpenData : opts.onOpenStaff;
    // Chipbreite auf die Knotenbreite deckeln -> ragt nie in Nachbarknoten;
    // die Beschriftung wird passend zur (ggf. gedeckelten) Breite gekuerzt.
    const rawW = Math.round(chip.label.length * CHIP_CH) + CHIP_PADX * 2;
    const w = Math.min(rawW, p.w);
    const cx = p.x + (p.w - w) / 2;
    const maxChars = Math.max(3, Math.floor((w - CHIP_PADX * 2) / CHIP_CH));
    const g = svg("g", {
      class: "gchip gchip-" + chip.kind + (onOpen ? " gchip-link" : ""),
      onClick: onOpen ? (e) => { e.stopPropagation(); onOpen(node.id); } : null,
    });
    // Wohin der Klick fuehrt, entscheidet die aufrufende Sicht (Karten-Sicht:
    // der passende Abschnitt der Schritt-Karte; klassisch: die Daten-/
    // Ressourcensicht) -- der Hinweis bleibt deshalb bewusst neutral.
    const hint = onOpen
      ? (chip.kind === "data" ? " \u2013 klicken zeigt die Datenbindungen" : " \u2013 klicken zeigt die Bearbeiterzuordnung")
      : "";
    g.appendChild(svg("title", null, document.createTextNode(chip.title + hint)));
    g.appendChild(svg("rect", { class: "gchip-bg", x: cx, y, width: w, height: CHIP_H, rx: 8 }));
    g.appendChild(svg("text", { class: "gchip-txt", x: cx + w / 2, y: y + 11, "text-anchor": "middle" },
      document.createTextNode(truncate(chip.label, maxChars))));
    root.appendChild(g);
    y += CHIP_H + CHIP_GAP;
  });
}

// Geometrie des Schnellrings am gewaehlten Knoten. Der Ring liegt UEBER dem Knoten, weil unter ihm bereits
// der Badge-Stapel haengt (siehe renderNodeBadges).
const RING_R = 11, RING_GAP = 7, RING_LIFT = 24;

/**
 * Die Aktionen des Schnellrings fuer einen Knoten -- rein deklarativ.
 *
 * Gemeinsame Quelle fuer das Zeichnen (renderNodeRing) und die
 * viewBox-Erweiterung in renderGraph, damit beide dieselbe Anzahl sehen. An
 * Start-, End- und Join-Knoten faellt der Ring ganz weg (dort gibt es nichts zu
 * tun; Joins werden ueber ihren oeffnenden Split entfernt).
 *
 * @param {object} schema Das Schema (fuer die Erkennung eines leeren XOR-Zweigs).
 * @param {object} node   Der Knoten.
 * @returns {Array<{key: string, symbol: string, title: string}>} Aktionen, ggf. leer.
 */
function nodeRingActions(schema, node) {
  if (!node) return [];
  if (node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS) {
    return [
      { key: "insert", symbol: "+", title: "Schritt danach einfügen" },
      { key: "rename", symbol: "✎", title: "Bezeichnung ändern" },
      { key: "bind", symbol: "⊕", title: "Datenelement binden" },
      { key: "delete", symbol: "✕", title: "Schritt entfernen" },
    ];
  }
  if (SPLIT_TYPES.has(node.type)) {
    const acts = [];
    if (emptyBranchJoin(schema, node.id)) {
      acts.push({ key: "empty-branch", symbol: "⌫", title: "Leeren Zweig entfernen" });
    }
    acts.push({ key: "delete", symbol: "✕", title: "Verzweigung entfernen" });
    return acts;
  }
  return [];
}

/**
 * Zeichnet den Schnellring des gewaehlten Knotens als Teil des SVG.
 *
 * Bewusst IM SVG und nicht als HTML-Overlay: Pan/Zoom verschiebt den Graphen
 * ueber eine CSS-Transformation, ein separat positioniertes Overlay muesste
 * jede Bewegung nachfuehren (oder verlöre den Bezug). Als SVG-Element wandert
 * der Ring automatisch mit seinem Knoten mit.
 *
 * Wird nur gezeichnet, wenn die aufrufende Sicht ``opts.onNodeAction`` setzt --
 * Ausfuehrung, Monitoring und Pruefinstanz nutzen dieselbe renderGraph-Funktion
 * und bleiben damit unveraendert.
 *
 * @param {SVGElement} root Wurzel-SVG.
 * @param {object} schema   Das Schema.
 * @param {object} node     Der zu bedienende Knoten.
 * @param {{x:number,y:number,w:number,h:number}} p Knotenrechteck aus layoutSchema.
 * @param {object} opts     renderGraph-Optionen (onNodeAction, selectedId).
 */
function renderNodeRing(root, schema, node, p, opts) {
  if (!opts.onNodeAction || opts.selectedId !== node.id) return;
  const acts = nodeRingActions(schema, node);
  if (!acts.length) return;
  const total = acts.length * (RING_R * 2) + (acts.length - 1) * RING_GAP;
  let cx = p.x + p.w / 2 - total / 2 + RING_R;
  const cy = p.y - RING_LIFT;
  acts.forEach((a) => {
    const g = svg("g", {
      class: "gring gring-" + a.key,
      style: "cursor:pointer",
      onClick: (e) => { e.stopPropagation(); opts.onNodeAction(node.id, a.key); },
    });
    g.appendChild(svg("title", null, document.createTextNode(a.title)));
    g.appendChild(svg("circle", { class: "gring-bg", cx, cy, r: RING_R }));
    g.appendChild(svg("text", { class: "gring-txt", x: cx, y: cy + 4, "text-anchor": "middle" },
      document.createTextNode(a.symbol)));
    root.appendChild(g);
    cx += RING_R * 2 + RING_GAP;
  });
}

/**
 * Setzt den Befund-Marker an einen Knoten, an dem der Kern etwas beanstandet.
 *
 * ``ValidationFinding`` traegt bereits ein optionales ``node_id`` -- der Client
 * gruppiert die Befunde nur (findingsByNode) und zeigt sie dort an, wo sie
 * entstehen, statt nur als Liste am Seitenrand. Er entscheidet dabei **nichts**:
 * angezeigt wird ausschliesslich, was der Kern geliefert hat.
 *
 * @param {SVGElement} root Wurzel-SVG.
 * @param {object} node     Der Knoten.
 * @param {{x:number,y:number,w:number,h:number}} p Knotenrechteck.
 * @param {object} opts     renderGraph-Optionen (findings, onFinding).
 */
function renderNodeFindingMark(root, node, p, opts) {
  const list = opts.findings ? opts.findings[node.id] : null;
  if (!list || !list.length) return;
  const cx = p.x + p.w - 9, cy = p.y + 9;
  const g = svg("g", {
    class: "gfind",
    style: opts.onFinding ? "cursor:pointer" : "",
    onClick: opts.onFinding ? (e) => { e.stopPropagation(); opts.onFinding(node.id); } : null,
  });
  g.appendChild(svg("title", null, document.createTextNode(
    list.map((f) => findingLine(f)).join("\n"))));
  g.appendChild(svg("circle", { class: "gfind-bg", cx, cy, r: 8 }));
  g.appendChild(svg("text", { class: "gfind-txt", x: cx, y: cy + 4, "text-anchor": "middle" },
    document.createTextNode(list.length > 1 ? String(list.length) : "!")));
  root.appendChild(g);
}

/**
 * Gruppiert die Befunde der letzten Validierung nach Knoten-Id.
 *
 * @returns {Object<string, Array<object>>} Knoten-Id -> Befunde (nur solche mit node_id).
 */
function findingsByNode() {
  const out = {};
  const v = state.validation;
  if (!v || !v.findings) return out;
  v.findings.forEach((f) => {
    if (!f.node_id) return;
    (out[f.node_id] = out[f.node_id] || []).push(f);
  });
  return out;
}

/** Befunde ohne Knotenbezug (modellweit, z. B. T2 kritischer Pfad). */
function globalFindings() {
  const v = state.validation;
  if (!v || !v.findings) return [];
  return v.findings.filter((f) => !f.node_id);
}

// Jump from the control flow into the data / resource view. Called from a node
// badge (``nodeId`` = that node, whose bindings are highlighted and scrolled
// into view) or from the "Vollansicht" button (``nodeId`` may be null -> no
// highlight). In both cases a **return point** is recorded so the user can jump
// straight back to where they left the control flow (see returnToControlFlow /
// returnBar). The highlight focus is mutually exclusive between the two views.
function focusBindingView(view, nodeId) {
  // Ausgangspunkt merken: aktuelle Ansicht + der Knoten, von dem aus gewechselt
  // wurde (der Badge-Knoten, sonst der aktuell gewählte Schritt).
  state.returnTo = { view: state.view, selectedNode: nodeId || state.selectedNode };
  state.dataFocusNode = view === "data" ? nodeId : null;
  state.staffFocusNode = view === "org" ? nodeId : null;
  state.view = view;
  setActiveNav();
  render();
}

// Ein-Klick-Rücksprung genau dorthin, wo der Kontrollfluss verlassen wurde:
// stellt die Ausgangsansicht wieder her, wählt den zuvor betrachteten Knoten
// erneut (die Modellieren-Sicht zentriert ihn nach dem Rendern) und räumt alle
// badge-getriebenen Hervorhebungen ab. Rein Navigations-UI.
function returnToControlFlow() {
  const r = state.returnTo;
  state.returnTo = null;
  state.dataFocusNode = null;
  state.staffFocusNode = null;
  state.orgFocusUnit = null;
  state.orgFocusAgents = [];
  state.view = r ? r.view : "model";
  if (r && r.selectedNode) state.selectedNode = r.selectedNode;
  setActiveNav();
  render();
}

// Rücksprung-Leiste: erscheint in der Daten-/Ressourcensicht, sobald ein
// Rücksprungpunkt gesetzt ist (also der Kontrollfluss per Klick verlassen
// wurde). Ein Klick führt exakt dorthin zurück. Liefert null, wenn es keinen
// Rücksprungpunkt gibt. Rein visuell -- kein Modell-/Backend-Zugriff.
function returnBar() {
  if (!state.returnTo) return null;
  const nodes = (state.schema && state.schema.nodes) || {};
  const node = state.returnTo.selectedNode && nodes[state.returnTo.selectedNode];
  const label = node
    ? "◀︎ Zurück zum Kontrollfluss – „" + nodeCaption(node) + "“"
    : "◀︎ Zurück zum Kontrollfluss";
  return el("div", { class: "return-bar" },
    el("button", { class: "btn small", onClick: returnToControlFlow, title: "Zurück zu der Stelle im Kontrollfluss, von der aus gewechselt wurde" }, label));
}

// Smoothly bring the first highlighted (.hl-row) table row of the current view
// into the centre of the viewport after a render.
function scrollHighlightIntoView() {
  requestAnimationFrame(() => {
    const row = byId("content").querySelector(".hl-row");
    if (row) row.scrollIntoView({ block: "center", behavior: "smooth" });
  });
}

// Dismissible banner shown above a highlighted binding table to explain why a
// row is emphasised and let the user clear the emphasis.
function focusBanner(text, onClear) {
  return el("div", { class: "focus-banner" },
    el("span", null, text),
    el("button", { class: "btn small ghost", onClick: onClear }, "Hervorhebung l\u00F6schen"));
}

// Highlight an organisational unit (in the Abteilungen tree and the org chart)
// together with every agent that belongs to it -- including the unit's
// supervisor (manager) -- in the Agenten table. Selecting a unit is a fresh
// resource-focus intent, so it clears any staff-rule highlight. Not persisted.
function focusOrgUnit(unitId) {
  const org = (state.schema && state.schema.org_model) || { agents: {}, org_units: {} };
  const unit = (org.org_units || {})[unitId];
  if (!unit) return;
  const ids = Object.values(org.agents || {})
    .filter((a) => a.org_unit_id === unitId)
    .map((a) => a.id);
  if (unit.manager_id && (org.agents || {})[unit.manager_id] && !ids.includes(unit.manager_id)) {
    ids.push(unit.manager_id);
  }
  state.orgFocusUnit = unitId;
  state.orgFocusAgents = ids;
  state.staffFocusNode = null;
  render();
}

// Highlight a single agent (a unit's supervisor) in the Agenten table -- used
// when the supervisor badge in the Abteilungen tree is clicked.
function focusOrgAgent(agentId, unitId) {
  const org = (state.schema && state.schema.org_model) || { agents: {} };
  if (!agentId || !(org.agents || {})[agentId]) return;
  state.orgFocusUnit = unitId || null;
  state.orgFocusAgents = [agentId];
  state.staffFocusNode = null;
  render();
}

// Make a rendered graph canvas pannable (drag in any direction) and zoomable
// (mouse wheel, anchored to the pointer position). Pan/zoom is purely visual
// (a CSS transform on the SVG) and resets on the next render -- it never
// touches the model or any backend state. The controller is exposed on
// ``wrap._panzoom`` so ``centerCanvasOnNode`` can re-centre the selected node.
function attachPanZoom(wrap, svgEl) {
  const MIN = 0.2, MAX = 4;
  let scale = 1, tx = 0, ty = 0;
  // Zweistufiges Einpassen (siehe fitToView): true, solange die lesbare Stufe
  // gezeigt wird und der naechste Klick die volle Uebersicht bringen soll. Jede
  // eigene Bewegung (Rad, Ziehen) setzt zurueck.
  const fitState = { readableShown: false };
  // Breite eines am rechten Rand liegenden Overlays (Modellieren-Sicht: die
  // Schritt-Karte), die beim Einpassen und Zentrieren frei bleiben muss.
  // Ohne diese Reserve zentriert die Canvas den gewaehlten Knoten exakt unter
  // die Karte -- man bearbeitet dann einen Schritt, den man nicht sieht.
  let reserveRight = 0;

  /** Mindestmassstab, den der erste Einpassen-Klick nicht unterschreitet. */
  const FIT_READABLE = 0.6;
  /**
   * Mindestmassstab der Uebersicht (zweiter Klick). Frueher bis MIN (0,2) --
   * ein Modell mit 16 Knoten war dann nicht mehr lesbar. Was bei
   * diesem Massstab nicht passt, bleibt seitlich verschiebbar; die Randpfeile
   * zeigen, dass dort noch etwas liegt.
   */
  const FIT_OVERVIEW_MIN = 0.35;

  // Randpfeile: „hier geht es weiter“. Ohne sie wirkte das lesbare Einpassen
  // wie ein abgeschnittenes Modell.
  const moreLeft = el("div", { class: "canvas-more canvas-more-left", "aria-hidden": "true" }, "\u25C0");
  const moreRight = el("div", { class: "canvas-more canvas-more-right", "aria-hidden": "true" }, "\u25B6 weiter rechts");
  wrap.appendChild(moreLeft);
  wrap.appendChild(moreRight);

  /**
   * Der tatsaechlich sichtbare Streifen der Canvas.
   *
   * Die Canvas ist ``clamp(420px, 66vh, 900px)`` hoch; auf Laptop-Hoehe (etwa
   * 1054 x 676) ragt sie unter den Fensterrand. Einpassen zentrierte deshalb
   * ueber eine Hoehe, von der ein Teil unsichtbar war, und die Randpfeile
   * sassen unten ausserhalb des Fensters. Ist der sichtbare Streifen zu schmal
   * (Canvas weit weggescrollt), gilt die volle Hoehe wie bisher.
   * @returns {{top: number, h: number}} Oberkante (relativ zur Canvas) und Hoehe
   */
  function visibleBand() {
    const full = { top: 0, h: wrap.clientHeight };
    if (typeof wrap.getBoundingClientRect !== "function" || typeof window === "undefined") return full;
    const r = wrap.getBoundingClientRect();
    // Sichtbar ist, was Fenster UND jeder scrollende Vorfahre (``.main``) zeigen;
    // die App macht der Demo-Leiste unten Platz, ``.main`` endet also darueber.
    let clipTop = 0, clipBottom = window.innerHeight || 0;
    for (let a = wrap.parentElement; a && a !== document.body; a = a.parentElement) {
      const oy = getComputedStyle(a).overflowY;
      if (oy === "visible") continue;
      const ar = a.getBoundingClientRect();
      clipTop = Math.max(clipTop, ar.top);
      clipBottom = Math.min(clipBottom, ar.bottom);
    }
    const top = Math.max(0, clipTop - r.top);
    const bottom = Math.min(wrap.clientHeight, clipBottom - r.top);
    const h = bottom - top;
    return h >= 160 ? { top, h } : full;
  }

  /**
   * Senkrechte Mitte des Start-Knotens in Modellkoordinaten, falls vorhanden.
   * Die lesbare Einpassen-Stufe legt die Hauptlinie ab Start in die Mitte des
   * sichtbaren Streifens: Bei einem hohen Modell (Zweige ober- und unterhalb)
   * zeigte die obere Ausrichtung sonst zuerst leere Flaeche.
   * @returns {number|null}
   */
  function startCenterY() {
    const node = svgEl.querySelector && svgEl.querySelector('[data-node-id="start"]');
    if (!node || typeof node.getBBox !== "function") return null;
    try { const b = node.getBBox(); return b.height ? b.y + b.height / 2 : null; } catch (_e) { return null; }
  }

  function apply() {
    svgEl.style.transformOrigin = "0 0";
    svgEl.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
    const vw = wrap.clientWidth;
    if (vb && vb.width && vw) {
      const left = tx + vb.x * scale;
      const right = tx + (vb.x + vb.width) * scale;
      moreLeft.style.display = left < -4 ? "" : "none";
      moreRight.style.display = right > vw - reserveRight + 4 ? "" : "none";
      moreRight.style.right = `${10 + reserveRight}px`;  // nicht unter der Schritt-Karte
      // Pfeile am unteren Rand des *sichtbaren* Streifens, nicht der Canvas.
      const band = visibleBand();
      const lift = Math.max(0, wrap.clientHeight - (band.top + band.h));
      moreLeft.style.bottom = moreRight.style.bottom = `${8 + lift}px`;
    }
  }
  apply();

  // Wheel handling:
  //  * Pinch-to-zoom (trackpad) and Ctrl+wheel (mouse) arrive with ctrlKey and
  //    zoom towards / away from the pointer (model point under the cursor stays
  //    fixed while the scale changes).
  //  * A plain two-finger trackpad swipe (or mouse wheel) pans the canvas -- in
  //    both directions, so sideways scrolling works. Shift+wheel maps a
  //    vertical mouse wheel to horizontal panning.
  wrap.addEventListener("wheel", (e) => {
    fitState.readableShown = false;  // eigene Bewegung: Einpassen beginnt wieder lesbar
    e.preventDefault();
    if (e.ctrlKey) {
      const rect = wrap.getBoundingClientRect();
      const px = e.clientX - rect.left, py = e.clientY - rect.top;
      const cx = (px - tx) / scale, cy = (py - ty) / scale;
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      const next = Math.min(MAX, Math.max(MIN, scale * factor));
      if (next === scale) return;
      scale = next;
      tx = px - cx * scale;
      ty = py - cy * scale;
      apply();
      return;
    }
    let dx = e.deltaX, dy = e.deltaY;
    if (e.shiftKey && dx === 0) { dx = dy; dy = 0; }
    tx -= dx; ty -= dy;
    apply();
  }, { passive: false });

  // Drag = pan. Only capture the pointer once movement passes a small
  // threshold so a plain click still selects a node / hits a "+" handle.
  let down = false, dragging = false, sx = 0, sy = 0, lastX = 0, lastY = 0;
  wrap.addEventListener("pointerdown", (e) => {
    // Eigene Bewegung: Einpassen beginnt wieder lesbar -- ausser beim Klick auf
    // den Einpassen-Knopf selbst, der im Canvas liegt (sonst kaeme die zweite
    // Stufe nie).
    if (!(e.target && e.target.closest && e.target.closest(".canvas-fit"))) fitState.readableShown = false;
    if (e.button !== 0) return;
    down = true; dragging = false;
    sx = lastX = e.clientX; sy = lastY = e.clientY;
  });
  wrap.addEventListener("pointermove", (e) => {
    if (!down) return;
    if (!dragging && Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) < 4) return;
    if (!dragging) {
      dragging = true;
      wrap.classList.add("grabbing");
      // Textselektion seitenweit unterdruecken, solange gezogen wird: sonst
      // markiert der Zeiger beim Verlassen des Canvas-Fensters die darunter
      // liegenden Seiteninhalte. Eine bereits (in den ersten Pixeln) begonnene
      // Selektion wird zusaetzlich geleert.
      document.documentElement.classList.add("graph-dragging");
      const sel = window.getSelection && window.getSelection();
      if (sel && sel.removeAllRanges) sel.removeAllRanges();
      try { wrap.setPointerCapture(e.pointerId); } catch (_e) { /* ignore */ }
    }
    tx += e.clientX - lastX; ty += e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    apply();
  });
  function endDrag(e) {
    if (!down) return;
    down = false;
    document.documentElement.classList.remove("graph-dragging");
    if (dragging) {
      wrap.classList.remove("grabbing");
      try { wrap.releasePointerCapture(e.pointerId); } catch (_e) { /* ignore */ }
    }
  }
  wrap.addEventListener("pointerup", endDrag);
  wrap.addEventListener("pointercancel", endDrag);
  // Swallow the click that trails a real drag so panning never selects a node.
  wrap.addEventListener("click", (e) => {
    if (dragging) { e.stopPropagation(); e.preventDefault(); }
    dragging = false;
  }, true);

  // Doppelklick mit der **mittleren** Maustaste rueckt das ganze Modell wieder
  // ins Bild -- die Tastatur-lose Notbremse, wenn man sich beim Verschieben
  // verloren hat. Zwei Dinge sind hier wichtig:
  //  * Der Browser (Windows/Linux) startet auf `mousedown` mit der mittleren
  //    Taste den Autoscroll-Modus; ohne `preventDefault` klebt danach ein
  //    Scroll-Anker am Zeiger.
  //  * Ein echter `dblclick` feuert nur fuer die linke Taste, deshalb zaehlen
  //    wir die mittleren Klicks selbst (Doppelklick-Fenster wie im System
  //    ueblich ~400 ms; ein zu langsamer zweiter Klick zaehlt als neuer
  //    erster, nicht als halber Doppelklick).
  const MIDDLE_DBL_MS = 400;
  let midClicks = 0, midAt = 0;
  wrap.addEventListener("mousedown", (e) => { if (e.button === 1) e.preventDefault(); });
  wrap.addEventListener("auxclick", (e) => {
    if (e.button !== 1) return;
    e.preventDefault();
    const now = Date.now();
    midClicks = now - midAt <= MIDDLE_DBL_MS ? midClicks + 1 : 1;
    midAt = now;
    if (midClicks >= 2) { midClicks = 0; fitToView(); }
  });

  // Setzt Zoom und Verschiebung so, dass das **gesamte** Modell sichtbar und
  // zentriert im Fenster liegt. Bezugsrahmen ist die viewBox des SVG (sie kann
  // fuer die Datenherkunft-Boegen nach oben erweitert sein, also einen
  // negativen Ursprung haben) plus ein kleiner Rand. Kleine Modelle werden
  // **nicht** ueber ihre natuerliche Groesse hinaus vergroessert (Deckel 1),
  // sonst wirkt ein Zwei-Knoten-Prozess nach dem Einpassen aufgeblasen.
  function fitToView() {
    const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
    const bx = vb && vb.width ? vb.x : 0;
    const by = vb && vb.width ? vb.y : 0;
    const bw = vb && vb.width ? vb.width : svgEl.clientWidth;
    const bh = vb && vb.height ? vb.height : svgEl.clientHeight;
    const band = visibleBand();
    const vw = wrap.clientWidth, vh = band.h;
    if (!bw || !bh || !vw || !vh) return;
    const MARGIN = 16;
    const vwFree = Math.max(120, vw - reserveRight);
    const fit = Math.min((vwFree - MARGIN * 2) / bw, (vh - MARGIN * 2) / bh);
    // Lesbar statt winzig: Passt das Modell nur unterhalb von FIT_READABLE ins
    // Bild, haelt der erste Klick diese Mindestgroesse und zeigt den Ablauf ab
    // dem Start (links); erst ein zweiter Klick direkt danach zeigt die volle
    // Uebersicht. Vorher war ein Modell mit elf Schritten nach dem Einpassen
    // kaum lesbar. Kleine Modelle verhalten sich unveraendert.
    const overview = fitState.readableShown;
    if (fit < FIT_READABLE && !overview) {
      scale = FIT_READABLE;
      tx = MARGIN - bx * scale;
      const h = bh * scale;
      if (h <= vh - MARGIN * 2) {
        ty = band.top + (vh - h) / 2 - by * scale;
      } else {
        // Hauptlinie ab Start mittig, aber keine Leerflaeche ueber der Ober-
        // bzw. unter der Unterkante des Modells.
        const cy = startCenterY();
        const topAligned = band.top + MARGIN - by * scale;
        const bottomAligned = band.top + vh - MARGIN - (by + bh) * scale;
        ty = cy == null ? topAligned
          : Math.min(topAligned, Math.max(bottomAligned, band.top + vh / 2 - cy * scale));
      }
      fitState.readableShown = true;
    } else {
      scale = Math.min(MAX, Math.max(Math.max(MIN, FIT_OVERVIEW_MIN), Math.min(1, fit)));
      const w = bw * scale;
      // Passt es auch so nicht, beginnt die Ansicht links am Start statt
      // mittig abgeschnitten zu sein; der Rest ist per Ziehen erreichbar.
      tx = w <= vwFree - MARGIN * 2 ? (vwFree - w) / 2 - bx * scale : MARGIN - bx * scale;
      const h = bh * scale;
      ty = band.top + (h <= vh - MARGIN * 2 ? (vh - h) / 2 - by * scale : MARGIN - by * scale);
      fitState.readableShown = false;
    }
    apply();
  }

  wrap._panzoom = {
    fitToView,
    /**
     * Meldet die Breite eines rechts liegenden Overlays (Schritt-Karte), die
     * beim Einpassen/Zentrieren frei bleiben soll. ``0`` hebt die Reserve auf
     * (z. B. mobile Bodensheet-Darstellung, die den Canvas nicht seitlich
     * verdeckt).
     *
     * @param {number} px Reservierte Breite in Bildschirmpixeln.
     */
    setReserve(px) { reserveRight = Math.max(0, px || 0); },
    /**
     * Rueckt den gewaehlten Knoten -- und optional einen zusaetzlichen Bereich,
     * der zu ihm gehoert -- ins Bild.
     *
     * @param {{x:number,y:number,w:number,h:number}|null} pos Knotenrechteck in
     *        Modellkoordinaten (darf fehlen, wenn nur ``region`` interessiert).
     * @param {{x0:number,y0:number,x1:number,y1:number}|null} region Zusaetzlich
     *        sichtbar zu haltender Bereich -- in der Modellieren-Sicht die
     *        gestrichelte Datenherkunft samt ihrer **Quellknoten**.
     *
     * Ohne ``region`` bleibt es beim reinen Zentrieren im aktuellen Massstab.
     * Mit ``region`` wird der Massstab bei Bedarf **verkleinert** (nie
     * vergroessert, nie ueber 1), bis der ganze Bereich hineinpasst: Der
     * Schreiber eines gelesenen Datenelements liegt meist mehrere hundert Pixel
     * weiter links, ein Zentrieren allein auf den Leseknoten schob ihn samt
     * Bogen und Beschriftung aus dem (overflow:hidden) Canvas -- die Herkunft
     * war gezeichnet, aber unsichtbar.
     */
    centerOn(pos, region) {
      // Die viewBox kann fuer die Datenherkunft-Boegen nach oben erweitert
      // sein (negativer viewBox-Ursprung); der Knoten liegt dann um |vbY|
      // tiefer im Pixelraum. Diesen Versatz beim Zentrieren kompensieren,
      // sonst springt der gewaehlte Knoten aus der Mitte.
      const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
      const offX = vb ? vb.x : 0, offY = vb ? vb.y : 0;
      const vw = wrap.clientWidth, vh = wrap.clientHeight;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      if (pos) {
        x0 = pos.x; y0 = pos.y; x1 = pos.x + pos.w; y1 = pos.y + pos.h;
      }
      if (region) {
        x0 = Math.min(x0, region.x0); y0 = Math.min(y0, region.y0);
        x1 = Math.max(x1, region.x1); y1 = Math.max(y1, region.y1);
      }
      if (!(x1 > x0) || !(y1 > y0)) return;
      const vwFree = Math.max(120, vw - reserveRight);
      if (region && vw > 0 && vh > 0) {
        const MARGIN = 24;
        const fit = Math.min((vwFree - MARGIN * 2) / (x1 - x0), (vh - MARGIN * 2) / (y1 - y0));
        scale = Math.min(scale, Math.max(MIN, Math.min(1, fit)));
      }
      const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
      tx = vwFree / 2 - (cx - offX) * scale;
      ty = vh / 2 - (cy - offY) * scale;
      apply();
    },
  };
}

/**
 * Anzeigetext einer Kantenbedingung.
 *
 * Der Kern leitet die Bedingung als technischen Text ab („Entscheidung:
 * otherwise“, „Freigabe == true“); der BPMN-Import liest genau diesen Text
 * zurueck, er bleibt deshalb unveraendert gespeichert. Nur die Anzeige wird
 * deutsch.
 * @param {string} text Bedingung wie gespeichert
 * @returns {string}
 */
function conditionCaption(text) {
  return String(text)
    .replace(/:\s*otherwise$/, ": sonst")
    .replace(/ == true\b/g, " = Ja")
    .replace(/ == false\b/g, " = Nein")
    .replace(/ or /g, " oder ");
}

function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "\u2026" : s; }
// --------------------------------------------------------------------------
// Laden / Auswahl
// --------------------------------------------------------------------------

async function loadSchemas() {
  state.schemaIds = await api.get("/schemas");
  // The list endpoint returns only IDs; fetch each name + version once so the
  // picker can show the human-readable schema name plus its revision (e.g.
  // "Urlaubsantrag (v2)") instead of the raw ID. Revisions share the same name
  // but carry a fresh ID and an incremented version, so the version makes them
  // distinguishable in the selection.
  const unknown = state.schemaIds.filter((id) => !(id in state.schemaNames));
  if (unknown.length) {
    const entries = await Promise.all(unknown.map(async (id) => {
      try { const s = await api.get(`/schemas/${id}`); return [id, s.name, s.version]; }
      catch (_e) { return [id, id, null]; }
    }));
    for (const [id, name, version] of entries) {
      state.schemaNames[id] = name;
      if (version != null) state.schemaVersions[id] = version;
    }
  }
  if (state.schemaIds.length && !state.schemaIds.includes(state.schemaId)) {
    state.schemaId = state.schemaIds[0];
  }
  if (!state.schemaIds.length) state.schemaId = null;
}

/**
 * Laedt die Namen von Prozessen nach, die seit dem Anmelden entstanden sind.
 *
 * ``loadSchemas`` holt Namen nur beim Laden der Liste; ein Prozess, den jemand
 * anderes spaeter freigab, stand in „Meine Aufgaben“ und im Monitoring bis zum
 * Neuladen als ``schema_245 (v1)``. Die Sichten, die ueber mehrere Prozesse
 * reichen, rufen dies vor dem Zeichnen mit den ids auf, die sie anzeigen.
 * @param {string[]} ids anzuzeigende Schema-ids
 * @returns {Promise<void>}
 */
async function ensureSchemaNames(ids) {
  const missing = [...new Set(ids)].filter((id) => id && !(id in state.schemaNames));
  if (!missing.length) return;
  await Promise.all(missing.map(async (id) => {
    try {
      const s = await api.get(`/schemas/${id}`);
      state.schemaNames[id] = s.name;
      state.schemaVersions[id] = s.version;
    } catch (_e) { /* nicht lesbar: dann bleibt die id stehen */ }
  }));
}

async function refreshSchema() {
  if (!state.schemaId) { state.schema = null; state.validation = null; state.hints = []; return; }
  state.schema = await api.get(`/schemas/${state.schemaId}`);
  state.validation = await api.get(`/schemas/${state.schemaId}/validation`);
  // Modellhinweise sind rein beratend – ein Fehler hier darf das Modellieren
  // nie blockieren (best-effort, leer statt Fehlermeldung).
  try {
    const report = await api.get(`/schemas/${state.schemaId}/metrics`);
    state.hints = (report && report.hints) || [];
  } catch (err) { state.hints = []; }
  // Migrationsassistent: nur fuer freigegebene Revisionen und nur fuer Rollen,
  // die migrieren duerfen. Best effort wie die Hinweise -- ohne Bericht fehlt
  // nur der Knopf, das Modellieren bleibt unberuehrt.
  state.migrationReport = null;
  if (state.schema && state.schema.lifecycle_state === "RELEASED" && hasRole("operator", "modeler", "admin")) {
    try { state.migrationReport = await api.get(`/schemas/${state.schemaId}/migration-report`); }
    catch (err) { state.migrationReport = null; }
  }
  localStorage.setItem("schemaId", state.schemaId);
}

/**
 * Fuehrt EINE Modell-Operation am Kern aus und zeigt ihr Ergebnis an.
 *
 * Das gemeinsame Geruest fast aller Bindungs-, Knoten- und Organisations-
 * aenderungen: Aufruf absetzen, bei Erfolg das Schema neu laden
 * (``refreshSchema``), neu zeichnen und eine Erfolgsmeldung zeigen; bei einer
 * Ablehnung (422/409/403) die Meldung des Kerns ueber ``toastError``. Ob die
 * Aenderung zulaessig ist, entscheidet allein der Kern (Validate-before-Commit)
 * -- der Client prueft hier nichts.
 *
 * Der Rueckgabewert passt direkt auf den Bestaetigen-Rueckruf von
 * ``openModal``: ``false`` laesst den Dialog nach einer Ablehnung offen, damit
 * die Eingabe korrigiert werden kann; ``true`` schliesst ihn.
 *
 * @param {function(): Promise<*>} call Setzt den API-Aufruf ab. Als Funktion
 *   uebergeben, damit ein Fehler erst hier im ``try`` entsteht.
 * @param {string} okTitle Titel der Erfolgsmeldung.
 * @param {string[]} [okLines] Optionale Detailzeilen der Erfolgsmeldung.
 * @returns {Promise<boolean>} ``true`` nach Erfolg, ``false`` nach einem
 *   Fehler (auch wenn erst das Neuladen des Schemas scheitert).
 */
async function commitSchemaChange(call, okTitle, okLines) {
  try {
    await call();
    await refreshSchema(); render(); toast("ok", okTitle, okLines);
    return true;
  } catch (err) {
    toastError(err);
    return false;
  }
}

async function selectSchema(id) {
  state.schemaId = id;
  state.selectedNode = null;
  await refreshSchema();
  render();
}

/**
 * Ein soeben erzeugtes Schema oeffnen -- in der Modellieren-Sicht.
 *
 * Anlegen, BPMN-Import und "Aus Vorlage" sind von jeder Sicht aus erreichbar
 * (Schema-Umschalter in der Kopfzeile). Nur zu waehlen liess die Ansicht dort
 * stehen, wo man gerade war (z. B. "Meine Aufgaben"), und das neue Modell blieb
 * unsichtbar.
 * @param {string} id Schema-ID
 */
async function openCreatedSchema(id) {
  state.view = "model";
  setActiveNav();
  await selectSchema(id);
}

/**
 * Leerzustand der Modellieren-Sicht (beide Oberflaechen): Umlaute statt
 * „ausgewaehlt“ und gleich die naechsten Schritte als Knoepfe. „Beispieldaten
 * laden“ nur fuer Admins und nur, wenn das System noch **leer** ist -- das
 * Laden loescht vorher alles (``confirmReset`` fragt zusaetzlich nach).
 * @returns {HTMLElement}
 */
function noSchemaState() {
  const box = emptyState(state.schemaIds.length
    ? "Kein Schema ausgew\u00E4hlt. Oben ein Schema w\u00E4hlen oder ein neues anlegen."
    : "Noch kein Prozess vorhanden.");
  const actions = el("div", { class: "btn-row", style: "justify-content:center;margin-top:10px" },
    hasRole("modeler", "admin")
      ? el("button", { class: "btn small primary", onClick: newSchema }, "Neuer Prozess")
      : null,
    hasRole("modeler", "admin")
      ? el("button", { class: "btn small ghost", onClick: newFromTemplate }, "Aus Vorlage")
      : null,
    hasRole("admin") && !state.schemaIds.length
      ? el("button", { class: "btn small ghost", onClick: () => confirmReset("demo") }, "Beispieldaten laden")
      : null);
  box.appendChild(actions);
  return box;
}

function activitiesOf(schema) {
  return Object.values(schema.nodes).filter((n) => n.type === NODE_TYPE.ACTIVITY);
}
function isDraft(schema) { return schema && schema.lifecycle_state === "ENTWURF"; }

function lifecyclePill(schema) {
  const s = schema.lifecycle_state;
  const cls = s === "RELEASED" ? "pill-green" : s === "ENTWURF" ? "pill-amber" : "pill-gray";
  return el("span", { class: "pill " + cls, title: s }, lifecycleLabel(s));
}

// --------------------------------------------------------------------------
// Topbar / Schema-Picker
// --------------------------------------------------------------------------

// Human-readable schema caption including its revision, e.g. "Urlaubsantrag
// (v2)". Revisions share the same name but get a fresh id and an incremented
// version, so the version is what makes them distinguishable. ``version`` may
// be passed explicitly (e.g. an instance's own schema_version, which is robust
// even when the schema is no longer in the picker); otherwise it falls back to
// the cached version for that id. The id is used as a last resort.
function schemaLabel(id, version) {
  const name = state.schemaNames[id] || id;
  const v = version != null ? version : state.schemaVersions[id];
  return v != null ? `${name} (v${v})` : name;
}

function renderSchemaPicker() {
  const picker = byId("schema-picker");
  clear(picker);
  const select = el("select", { onChange: (e) => selectSchema(e.target.value) },
    ...state.schemaIds.map((id) => {
      const o = el("option", { value: id }, schemaLabel(id));
      if (id === state.schemaId) o.selected = true;
      return o;
    }));
  if (!state.schemaIds.length) {
    select.appendChild(el("option", null, "(kein Schema)"));
    select.disabled = true;
  }
  picker.appendChild(select);
  // Anlegen, Vorlage und Import nur fuer Modellierer/Admin -- ein Bearbeiter
  // bekam nach dem Klick nur „forbidden“. Der Server bleibt
  // maßgeblich; das hier blendet nur aus, was ohnehin abgelehnt wuerde.
  if (!hasRole("modeler", "admin")) return;
  picker.appendChild(el("button", { class: "btn small", onClick: newSchema }, "+ Neu"));
  picker.appendChild(el("button", { class: "btn small ghost", onClick: newFromTemplate,
    title: "Neues Schema aus einer Vorlage erstellen" }, "Aus Vorlage"));
  picker.appendChild(el("button", { class: "btn small ghost", onClick: importBpmn }, "BPMN-Import"));
}

async function newSchema() {
  const nameInput = el("input", { type: "text", placeholder: "z. B. Urlaubsantrag" });
  openModal("Neues Schema", el("label", { class: "field" }, "Name", nameInput), async () => {
    const name = nameInput.value.trim();
    if (!name) return false;
    try {
      const schema = await api.post("/schemas", { name });
      await loadSchemas();
      await openCreatedSchema(schema.id);
      toast("ok", "Schema angelegt", [schema.name]);
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * BPMN-2.0-Import: Datei waehlen oder XML einfuegen.
 *
 * Die Datei wird nur im Browser gelesen (FileReader) und landet im selben
 * Textfeld -- derselbe Endpunkt, dieselbe Pruefung im Kern, keine neue
 * Serverflaeche. Der Name wird aus dem Dateinamen
 * vorbelegt, solange er noch leer ist.
 */
async function importBpmn() {
  const ta = el("textarea", { placeholder: "<bpmn:definitions ...>" });
  const nameInput = el("input", { type: "text", placeholder: "optionaler Name" });
  const fileInput = el("input", { type: "file", class: "bpmn-file", accept: ".bpmn,.xml,application/xml,text/xml" });
  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      ta.value = String(reader.result || "");
      if (!nameInput.value.trim()) nameInput.value = file.name.replace(/\.(bpmn|xml)$/i, "");
    };
    reader.onerror = () => toast("err", "Datei konnte nicht gelesen werden", [file.name]);
    reader.readAsText(file);
  });
  openModal("BPMN 2.0 importieren", el("div", { class: "row", style: "flex-direction:column;align-items:stretch" },
    el("label", { class: "field" }, "BPMN-Datei (.bpmn, .xml)", fileInput),
    el("label", { class: "field" }, "Name (optional)", nameInput),
    el("label", { class: "field" }, "BPMN-XML (aus der Datei oder eingefügt)", ta)), async () => {
    const xml = ta.value.trim();
    if (!xml) return false;
    try {
      const schema = await api.post("/bpmn-import", { xml, name: nameInput.value.trim() || null });
      await loadSchemas();
      await openCreatedSchema(schema.id);
      toast("ok", "BPMN importiert", [schema.name]);
    } catch (err) { toastError(err); return false; }
  }, "Importieren");
}

// --------------------------------------------------------------------------
// Prozessvorlagen (Templates): eingebaute Bibliothek + eigene Vorlagen
// --------------------------------------------------------------------------

// Gallery to start a new schema from a template. Fetches the merged list
// (built-in + user templates), groups it by category and lets the modeller
// instantiate one -- the server deep-copies the blueprint into a fresh draft.
async function newFromTemplate() {
  let templates;
  try {
    templates = await api.get("/templates");
  } catch (err) { toastError(err); return; }
  if (!templates.length) {
    toast("info", "Keine Vorlagen", ["Es sind noch keine Vorlagen vorhanden."]);
    return;
  }

  // One card per template; the selected card is remembered for the confirm.
  const selection = { id: null, name: null };
  const cards = templates.map((t) => {
    const badge = t.origin === "BUILTIN"
      ? el("span", { class: "pill pill-blue" }, "Vorlage")
      : el("span", { class: "pill pill-green" }, "Eigene");
    const card = el("button", { class: "tpl-card", "data-id": t.id, onClick: () => {
      selection.id = t.id; selection.name = t.name;
      // Ueber die ganze Galerie: eigene Vorlagen stecken in einer Huelle (.tpl-item).
      [...gallery.querySelectorAll(".tpl-card")].forEach((c) => c.classList.toggle("selected", c === card));
    } },
      el("div", { class: "tpl-card-h" },
        el("strong", null, t.name),
        badge),
      t.category ? el("div", { class: "tpl-cat" }, t.category + (t.step_count ? ` · ${t.step_count} Schritte` : "")) : null,
      el("div", { class: "tpl-desc" }, t.description || "—"),
      // Wer macht was: aus den Bearbeiterregeln des Bauplans abgeleitet (Kern:
      // templates.template_roles) -- passt damit immer zum Inhalt der Vorlage.
      (t.roles || []).length
        ? el("ul", { class: "tpl-roles" }, ...t.roles.map((r) =>
            el("li", null, el("strong", null, r.name), ": " + r.steps.join(", "))))
        : null);
    // Eigene Vorlagen lassen sich wieder loeschen (Modellierer-Anleitung §1a);
    // eingebaute gehoeren zum Produkt. Eigener Knopf unter der Karte -- die
    // Karte selbst ist schon ein Knopf (Auswahl).
    if (t.origin === "BUILTIN" || !hasRole("modeler", "admin")) return card;
    return el("div", { class: "tpl-item" }, card,
      el("button", { class: "btn small ghost tpl-del", type: "button",
        title: "Eigene Vorlage löschen", onClick: () => confirmTemplateDelete(t) }, "Löschen"));
  });
  const gallery = el("div", { class: "tpl-gallery" }, ...cards);

  openModal("Aus Vorlage erstellen", gallery, async () => {
    if (!selection.id) { toast("info", "Keine Vorlage gew\u00E4hlt", ["Bitte eine Vorlage ausw\u00E4hlen."]); return false; }
    try {
      const schema = await api.post(`/templates/${encodeURIComponent(selection.id)}/instantiate`,
        { name: selection.name });
      await loadSchemas();
      await openCreatedSchema(schema.id);
      toast("ok", "Aus Vorlage erstellt", [schema.name]);
    } catch (err) { toastError(err); return false; }
  }, "Erstellen");
}

/**
 * Rueckfrage und Loeschen einer eigenen Vorlage, danach wieder die Galerie.
 *
 * Der Kern lehnt eingebaute Vorlagen ab (422) und verlangt Modellierer/Admin;
 * Prozesse, die schon aus der Vorlage entstanden sind, bleiben unberuehrt.
 *
 * @param {{id: string, name: string}} t die Vorlage
 */
function confirmTemplateDelete(t) {
  openModal(`Vorlage l\u00F6schen: ${t.name}`,
    el("p", { class: "muted" },
      "Die Vorlage verschwindet aus der Galerie. Prozesse, die schon aus ihr erstellt wurden, bleiben unver\u00E4ndert."),
    async () => {
      try {
        await api.del(`/templates/${encodeURIComponent(t.id)}`);
        toast("ok", "Vorlage gel\u00F6scht", [t.name]);
        // Galerie ersetzt diesen Dialog; false, sonst schloesse openModal sie.
        newFromTemplate();
        return false;
      } catch (err) { toastError(err); return false; }
    }, "L\u00F6schen", { danger: true });
}

// Capture the current schema as a reusable *user* template. The server stores a
// self-contained, validated blueprint; only correct schemas can be captured.
async function saveAsTemplate() {
  const schema = state.schema;
  if (!schema) return;
  const nameInput = el("input", { type: "text", value: schema.name + " (Vorlage)" });
  const catInput = el("input", { type: "text", placeholder: "z. B. Personal, Einkauf, IT" });
  const descInput = el("textarea", { placeholder: "Kurzbeschreibung der Vorlage" });
  openModal("Als Vorlage speichern", el("div", { class: "row", style: "flex-direction:column;align-items:stretch" },
    el("label", { class: "field" }, "Name", nameInput),
    el("label", { class: "field" }, "Kategorie (optional)", catInput),
    el("label", { class: "field" }, "Beschreibung (optional)", descInput)), async () => {
    const name = nameInput.value.trim();
    if (!name) return false;
    try {
      const tpl = await api.post("/templates", {
        schema_id: schema.id, name,
        category: catInput.value.trim(), description: descInput.value.trim(),
      });
      toast("ok", "Als Vorlage gespeichert", [tpl.name]);
    } catch (err) { toastError(err); return false; }
  }, "Speichern");
}

// --------------------------------------------------------------------------
// View: Modellieren
// --------------------------------------------------------------------------

/**
 * Modellieren-Sicht: waehlt zwischen den beiden gleichwertigen Oberflaechen.
 *
 * * ``card``    -- Kontrollfluss als Arbeitsflaeche, alles zum gewaehlten
 *                  Schritt in einer Karte daneben (Standard).
 * * ``classic`` -- die urspruengliche Zwei-Spalten-Sicht mit Knoten-Inspektor
 *                  und Bindungs-Palette rechts.
 *
 * Beide laufen ueber **dieselben** API-Aufrufe; die Wahl ist reine Darstellung
 * und wird pro Browser gemerkt (localStorage ``modelUx``).
 */
function viewModel() {
  return modelUx() === "classic" ? viewModelClassic() : viewModelCard();
}

/** Aktive Modellier-Oberflaeche ("card" | "classic"); Default ist "card". */
function modelUx() {
  return state.modelUx === "classic" ? "classic" : "card";
}

/** Schaltet zwischen Karten- und klassischer Oberflaeche um (persistiert). */
function setModelUx(ux) {
  state.modelUx = ux === "classic" ? "classic" : "card";
  localStorage.setItem("modelUx", state.modelUx);
  // Vollbild ist an die Karten-Sicht gewoehnt (dort ist die Karte ein Overlay);
  // beim Wechsel zurueckfallen, damit die klassische Sicht nie ohne ihre
  // rechte Spalte dasteht.
  state.graphMaximized = false;
  render();
}

/**
 * Umschalter zwischen den beiden Modellier-Oberflaechen (Kopfzeile).
 *
 * @returns {HTMLElement} Der Knopf.
 */
function modelUxToggle() {
  const card = modelUx() === "card";
  return el("button", {
    class: "btn small ghost",
    title: card
      ? "Zur klassischen Ansicht wechseln (Knoten-Inspektor und Bindungs-Palette rechts)"
      : "Zur Karten-Ansicht wechseln (Kontrollfluss über die volle Breite, alles am gewählten Schritt)",
    onClick: () => setModelUx(card ? "classic" : "card"),
  }, card ? "▤ Klassische Ansicht" : "▦ Karten-Ansicht");
}

/**
 * Gemeinsame Kopfzeile beider Modellier-Oberflaechen (Name, Zustand, Aktionen).
 *
 * @param {object} schema Das aktuelle Schema.
 * @param {boolean} draft Ob es sich um einen Entwurf handelt.
 * @returns {HTMLElement} Das Kopf-Panel.
 */
function modelHeader(schema, draft) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, schema.name),
      el("span", { class: "sub" }, `v${schema.version}`),
      lifecyclePill(schema),
      validationBadge(),
      el("span", { class: "spacer", style: "flex:1" }),
      draft
        ? el("button", { class: "btn small ghost", onClick: setProcessDeadline,
            title: "Harte Frist für den gesamten Prozess (T2 – kritischer Pfad ≤ Termin)" },
            schema.deadline_seconds != null ? "Termin: " + formatDuration(schema.deadline_seconds) : "Termin …")
        : null,
      el("button", { class: "btn small ghost", onClick: exportBpmn }, "BPMN-Export"),
      hasRole("modeler", "admin")
        ? el("button", { class: "btn small ghost", onClick: saveAsTemplate,
            title: "Dieses Schema als wiederverwendbare Vorlage speichern" }, "Als Vorlage")
        : null,
      libraryToggleButton(schema),
      // Beim freigegebenen Schema fehlte in der Kopfzeile jeder Weg zurueck ins
      // Bearbeiten -- dort stand nur \u201EZur Ausfuehrung". Der Revisions-Knopf
      // steht deshalb daneben (dieselbe Operation wie im Panel unten).
      !draft && hasRole("modeler", "admin")
        ? el("button", { class: "btn small ghost", onClick: newRevision,
            title: "Bearbeitbare Entwurfskopie dieser Revision anlegen" }, "Neue Revision")
        : null,
      migrationHeaderButton(schema, draft),
      draft
        // Gruen nur, wenn freigabereif -- sonst sah der Knopf nach
        // "fertig" aus, obwohl Schritte ohne Bearbeiter die Freigabe verhindern.
        ? el("button", {
            class: "btn small" + (isReleasable(schema) ? " green" : ""),
            title: isReleasable(schema) ? "Schema freigeben (danach unver\u00E4nderlich)"
              : modelUx() === "classic"
                ? "Noch nicht freigabereif \u2013 siehe Panel \u201EKorrektheit\u201C"
                : "Noch nicht freigabereif \u2013 siehe Statusleiste",
            "data-tour": "model.release", onClick: releaseSchema }, "Freigeben")
        : el("button", { class: "btn small primary", onClick: () => { state.view = "run"; setActiveNav(); render(); } }, "Zur Ausf\u00FChrung"),
      draft && hasRole("modeler", "admin")
        ? el("button", { class: "btn small", onClick: startTestInstance, title: "Test-Instanz dieses Entwurfs starten und im 4-Quadranten-Cockpit durchspielen" }, "\u2697 Pr\u00FCfinstanz")
        : null,
      modelUxToggle()));
}

/**
 * Modellieren im Kontrollfluss: der Graph nutzt die volle Breite, die
 * Schritt-Karte erscheint als Overlay am gewaehlten Schritt.
 */
function viewModelCard() {
  const content = byId("content");
  // Der Fokus im Bezeichnungsfeld der Schritt-Karte muss VOR dem Leeren
  // gesichert werden: die Sicht baut ihr DOM bei jedem Rendern komplett neu auf,
  // und ein Hintergrund-Rendern (Tour-Tick) mitten im Tippen wuerde sonst Fokus
  // und Schreibmarke verschlucken.
  const nameFocus = captureCardNameFocus();
  clear(content);
  if (!state.schema) {
    content.appendChild(noSchemaState());
    return;
  }
  const schema = state.schema;
  const draft = isDraft(schema);
  const header = modelHeader(schema, draft);

  // Datenherkunft-Ueberlagerung: ein gezielt hervorgehobenes Element hat
  // Vorrang, sonst die Lesestellen des gewaehlten Knotens.
  const provenance = draft || state.selectedNode || state.dataElemFocus
    ? computeProvenance(schema, { selectedNode: state.selectedNode, dataElemFocus: state.dataElemFocus })
    : [];

  const graph = renderGraph(schema, {
    onPlus: draft ? openInsertModal : null,
    selectedId: state.selectedNode,
    onSelectNode: (id) => { state.selectedNode = id; state.cardFocusSection = null; render(); },
    // Ein Klick auf ein Knoten-Badge oeffnet den passenden Abschnitt der
    // Schritt-Karte -- am Element, statt in eine andere Sicht zu wechseln.
    // Der Weg in die Vollansicht bleibt ueber die Karte erhalten.
    onOpenData: (id) => openCardSection(id, "data"),
    onOpenStaff: (id) => openCardSection(id, "staff"),
    onNodeAction: draft ? nodeRingAction : null,
    findings: findingsByNode(),
    onFinding: (id) => openCardSection(id, "findings"),
    provenance,
  });

  // Kontrollfluss-Panel mit Maximieren-Knopf oben rechts. Im Vollbild wird das
  // Panel per CSS-Klasse ``graph-max`` zu einem bildschirmfuellenden Overlay
  // (zum gemeinsamen Diskutieren); der Knopf schaltet dann auf "verlassen".
  const maxBtn = el("button", {
    class: "btn small ghost graph-max-btn",
    title: state.graphMaximized ? "Vollbild verlassen (Esc)" : "Kontrollfluss maximieren",
    onClick: () => { state.graphMaximized = !state.graphMaximized; render(); },
  }, state.graphMaximized ? "× Verkleinern" : "⛶ Vollbild");
  // Der Kontrollfluss ist die Arbeitsflaeche: er nutzt die volle Breite, die
  // Schritt-Karte liegt als Overlay darueber und erscheint nur bei Auswahl.
  const graphBody = el("div", { class: "panel-b graph-body" }, graph);
  const card = stepCard(schema, draft);
  if (card) graphBody.appendChild(card);
  const empty = canvasEmptyState(schema, draft);
  if (empty) graphBody.appendChild(empty);
  const cfPanel = el("div", { class: "panel graph-panel model-canvas" + (state.graphMaximized ? " graph-max" : "") },
    el("div", { class: "panel-h" },
      el("h2", null, "Kontrollfluss"),
      el("span", { class: "spacer", style: "flex:1" }),
      maxBtn),
    graphBody,
    modelStatusBar(schema, draft));

  content.appendChild(header);
  content.appendChild(cfPanel);

  // Nach dem (Neu-)Rendern den ausgewaehlten Knoten in die Mitte der scrollbaren
  // Canvas ruecken, statt nach links auf den Start zurueckzuspringen -- und dabei
  // die gestrichelte Datenherkunft des Knotens mit ins Bild holen. Ohne diesen
  // zweiten Bezugspunkt zentrierte die Canvas allein auf den Leseknoten und schob
  // die Schreibseite (und damit den ganzen Bogen) aus dem sichtbaren Ausschnitt:
  // die Pfeile waren gezeichnet, aber nicht zu sehen.
  const focusPos = state.selectedNode ? layoutSchema(schema).pos[state.selectedNode] : null;
  if (focusPos || graph._provBounds) {
    requestAnimationFrame(() => {
      reserveCardWidth(graph, card);
      centerCanvasOnNode(graph, focusPos, graph._provBounds);
    });
  } else {
    requestAnimationFrame(() => reserveCardWidth(graph, card));
  }
  applyCardFocus(content, nameFocus);
}

/**
 * Meldet der Canvas, wie viel Breite die Schritt-Karte rechts belegt.
 *
 * Erst nach dem Einhaengen ins Dokument steht die tatsaechliche Breite fest
 * (sie haengt am CSS). Deckt die Karte fast die ganze Canvas ab, liegt die
 * mobile Bodensheet-Darstellung vor -- dann wird **keine** seitliche Reserve
 * gesetzt, weil die Karte dort unten und nicht rechts liegt.
 *
 * @param {HTMLElement} wrap Die Canvas (.canvas-wrap) mit ihrem Pan/Zoom-Controller.
 * @param {HTMLElement|null} card Die Schritt-Karte, falls sichtbar.
 */
function reserveCardWidth(wrap, card) {
  if (!wrap || !wrap._panzoom || !wrap._panzoom.setReserve) return;
  if (!card || !card.getBoundingClientRect) { wrap._panzoom.setReserve(0); return; }
  const cw = card.getBoundingClientRect().width;
  const vw = wrap.clientWidth || 0;
  wrap._panzoom.setReserve(vw && cw > vw * 0.75 ? 0 : cw + 12);
}

/**
 * Statusleiste unter dem Kontrollfluss: Freigabe-Ampel, modellweite Befunde
 * (die ohne Knotenbezug, z. B. T2 kritischer Pfad) und der Hinweistext.
 *
 * Ersetzt das frühere Panel „Korrektheit" der rechten Spalte. Knotenbezogene
 * Befunde stehen jetzt **am Knoten** (siehe renderNodeFindingMark) und in der
 * Schritt-Karte -- hier bleibt, was zu keinem einzelnen Schritt gehoert.
 *
 * @param {object} schema Das aktuelle Schema.
 * @param {boolean} draft Ob das Schema im Entwurf ist.
 * @returns {HTMLElement} Die Leiste.
 */
function modelStatusBar(schema, draft) {
  const v = state.validation;
  const globals = globalFindings();
  const bar = el("div", { class: "model-status", "data-tour": "model.findings" });
  bar.appendChild(v && v.correct
    ? el("span", { class: "pill pill-green" }, "✓ korrekt")
    : el("span", { class: "pill pill-red" }, countLabel(v ? v.findings.length : 0, "Befund", "Befunde")));
  // Stufe B getrennt von Stufe A: „korrekt" ist eine Invariante, „freigabereif"
  // ein Reifegrad. Ein Entwurf darf unfertig sein -- er soll es nur sehen, bevor
  // er freigibt und der Vorgang spaeter beim Sachbearbeiter stillsteht.
  const notReady = draft ? releaseFindings() : [];
  if (notReady.length) {
    bar.appendChild(el("span", {
      class: "pill pill-amber",
      title: notReady.map((f) => findingText(f)).join("\n"),
    }, `${countLabel(notReady.length, "Schritt", "Schritte")} ohne Bearbeiter`));
  }
  // Modellhinweise (G-Gruppe): rein beratend, dritter Zustand neben „korrekt"
  // und „freigabereif" – bewusst neutral (grau), nie rot/amber, damit ein
  // Hinweis nicht wie ein Befund wirkt. Details im Tooltip; in der klassischen
  // Sicht listet das Korrektheits-Panel dieselben Hinweise aus.
  const hints = draft ? (state.hints || []) : [];
  if (hints.length) {
    bar.appendChild(el("span", {
      class: "pill pill-gray",
      title: hints.map((h) => `${h.code}: ${hintText(h, schema)}`).join("\n"),
    }, countLabel(hints.length, "Hinweis", "Hinweise")));
  }
  const focusElem = state.dataElemFocus && schema.data_elements[state.dataElemFocus];
  const text = focusElem
    ? el("span", null,
        "Datenherkunft von „" + focusElem.name + "“: gestrichelte Linien zeigen, wo geschrieben → wo gelesen wird. ",
        el("a", { href: "#", onClick: (e) => { e.preventDefault(); state.dataElemFocus = null; render(); } }, "Hervorhebung löschen"))
    : el("span", null, draft
      ? "Einen Schritt anklicken: die Karte daneben trägt alles, was an ihm getan werden kann. „+“ an einer Kante oder am Schritt fügt einen weiteren ein. Unzulässiges weist der Kern ab."
      : "Schema ist freigegeben und damit unveränderlich. Einen Schritt anklicken zeigt seine Bindungen und – gestrichelt – woher seine gelesenen Daten stammen. Zum Bearbeiten eine neue Revision anlegen.");
  bar.appendChild(el("span", { class: "model-status-txt" }, text));
  // Die Tastaturwege findet ohne Hinweis niemand; deshalb stehen sie
  // hier, wo sie beim Modellieren im Blick sind (mobil ausgeblendet).
  bar.appendChild(el("span", { class: "model-status-key",
    title: "Pfeiltasten bewegen die Auswahl im Kontrollfluss (← → entlang des Ablaufs, ↑ ↓ zwischen Zweigen), Enter springt in die Karte, Esc hebt die Auswahl auf" },
    "← → Schritt · ↑ ↓ Zweig · ↵ Karte · Esc abwählen"));
  globals.forEach((f) => bar.appendChild(
    el("span", { class: "model-status-find", title: findingText(f, { withHint: true }) },
      el("span", { class: "rule" }, f.rule), findingText(f))));
  return bar;
}

/**
 * Einstiegskarte fuer einen noch leeren Prozess (nur Start → Ende).
 *
 * Der Weg ist derselbe wie sonst (``openInsertModal`` auf der einzigen Kante),
 * nur auffindbar: Vor der Umstellung stand hier eine leere Flaeche mit einem
 * kleinen „+“ auf der einzigen Verbindungslinie.
 *
 * @param {object} schema Das Schema.
 * @param {boolean} draft Ob bearbeitet werden darf.
 * @returns {HTMLElement|null} Die Karte, oder null (Prozess ist nicht leer).
 */
function canvasEmptyState(schema, draft) {
  if (!draft || activitiesOf(schema).length) return null;
  const start = Object.values(schema.nodes || {}).find((n) => n.type === NODE_TYPE.START);
  if (!start) return null;
  return el("div", { class: "canvas-empty" },
    el("div", { class: "canvas-empty-t" }, "Der Prozess ist noch leer"),
    el("div", { class: "canvas-empty-b" },
      "Zwischen Start und Ende passiert bisher nichts. Der erste Schritt legt fest, womit der Vorgang beginnt."),
    el("button", { class: "btn primary", onClick: () => openInsertModal(start.id) }, "Ersten Schritt anlegen"));
}

// --------------------------------------------------------------------------
// Modellieren (klassisch): zwei Spalten -- Kontrollfluss links, Knoten-
// Inspektor + Bindungs-Palette + Korrektheit + Schema-Evolution rechts.
//
// Bleibt bewusst **gleichwertig neben** der Karten-Sicht bestehen (Umschalter
// in der Kopfzeile): Beide bedienen dieselben API-Aufrufe, tragen also keine
// eigene Korrektheitslogik, und wer die gewohnte Palette bevorzugt, verliert
// nichts. Aenderungen an einer Bindungs-/Knoten-Operation wirken automatisch in
// beiden Sichten, weil sie dieselben Funktionen aufrufen.
// --------------------------------------------------------------------------

function viewModelClassic() {
  const content = byId("content");
  clear(content);
  if (!state.schema) {
    content.appendChild(noSchemaState());
    return;
  }
  const schema = state.schema;
  const draft = isDraft(schema);
  const header = modelHeader(schema, draft);

  // Datenherkunft-Ueberlagerung: ein in der Palette gewaehltes Element hat
  // Vorrang, sonst die Lesestellen des gewaehlten Knotens.
  const provenance = draft || state.selectedNode || state.dataElemFocus
    ? computeProvenance(schema, { selectedNode: state.selectedNode, dataElemFocus: state.dataElemFocus })
    : [];

  const graph = renderGraph(schema, {
    onPlus: draft ? openInsertModal : null,
    selectedId: state.selectedNode,
    onSelectNode: (id) => { state.selectedNode = id; render(); },
    onOpenData: (id) => focusBindingView("data", id),
    onOpenStaff: (id) => focusBindingView("org", id),
    provenance,
  });

  const focusElem = state.dataElemFocus && schema.data_elements[state.dataElemFocus];
  const hint = el("div", { class: "panel-b muted", style: "font-size:12px" },
    focusElem
      ? el("span", null,
          "Datenherkunft von „" + focusElem.name + "“: gestrichelte Linien zeigen, wo geschrieben → wo gelesen wird. ",
          el("a", { href: "#", onClick: (e) => { e.preventDefault(); state.dataElemFocus = null; render(); } }, "Hervorhebung löschen"))
      : draft
        ? "Geführtes Modellieren: „+“ an einer Kante fügt einen Schritt ein. Einen Schritt anklicken, dann rechts unter „Binden“ ein Datenelement/eine Ressource mit ⊕ zuweisen. Ein angeklickter Schritt zeigt außerdem gestrichelt, woher seine gelesenen Daten stammen. Unzulässiges weist der Kern ab."
        : "Schema ist freigegeben und damit unveränderlich. Einen Schritt anklicken zeigt gestrichelt, woher seine gelesenen Daten stammen; ein Klick auf ein Datenelement rechts zeigt alle seine Lesestellen. Zum Bearbeiten über „Neue Revision“ eine Entwurfskopie anlegen oder Instanzen starten.");

  // Kontrollfluss-Panel mit Maximieren-Knopf oben rechts. Im Vollbild wird das
  // Panel per CSS-Klasse ``graph-max`` zu einem bildschirmfuellenden Overlay
  // (zum gemeinsamen Diskutieren); der Knopf schaltet dann auf "verlassen".
  const maxBtn = el("button", {
    class: "btn small ghost graph-max-btn",
    title: state.graphMaximized ? "Vollbild verlassen (Esc)" : "Kontrollfluss maximieren",
    onClick: () => { state.graphMaximized = !state.graphMaximized; render(); },
  }, state.graphMaximized ? "× Verkleinern" : "⛶ Vollbild");
  const cfPanel = el("div", { class: "panel graph-panel" + (state.graphMaximized ? " graph-max" : "") },
    el("div", { class: "panel-h" },
      el("h2", null, "Kontrollfluss"),
      el("span", { class: "spacer", style: "flex:1" }),
      maxBtn),
    el("div", { class: "panel-b graph-body" }, graph),
    hint);

  content.appendChild(header);
  content.appendChild(el("div", { class: "grid-2 model-top" },
    cfPanel,
    el("div", null, nodeInspectorPanel(), bindingPalette(schema, draft), findingsPanel(), revisionPanel())));

  // Nach dem (Neu-)Rendern den ausgewaehlten Knoten in die Mitte der scrollbaren
  // Canvas ruecken, statt nach links auf den Start zurueckzuspringen -- und dabei
  // die gestrichelte Datenherkunft des Knotens mit ins Bild holen. Ohne diesen
  // zweiten Bezugspunkt zentrierte die Canvas allein auf den Leseknoten und schob
  // die Schreibseite (und damit den ganzen Bogen) aus dem sichtbaren Ausschnitt:
  // die Pfeile waren gezeichnet, aber nicht zu sehen.
  const focusPos = state.selectedNode ? layoutSchema(schema).pos[state.selectedNode] : null;
  if (focusPos || graph._provBounds) {
    requestAnimationFrame(() => centerCanvasOnNode(graph, focusPos, graph._provBounds));
  }
}

// --------------------------------------------------------------------------
// Modellieren-Seite: rechte Bindungs-Palette (Datenelemente + Ressourcen).
// Klick-zu-Binden (Safari-robust) statt Drag&Drop: zuerst links einen Schritt
// waehlen, dann in der Liste "An Schritt binden" (⊕) klicken. (Native HTML5-
// Drop-Events auf SVG-Knoten sind in Safari unzuverlaessig; Klicken ist stabil.)
// --------------------------------------------------------------------------

// Baustein: ein Paletten-Chip (eine Listenzeile) mit Klick (z. B. Herkunft
// hervorheben) und rechtsbuendigen Aktionen (Binden/Bearbeiten). Rein
// praesentational -- veraendert nie Modell oder Backend.
function paletteChip(opts) {
  const chip = el("div", {
    class: "pal-chip" + (opts.active ? " active" : ""),
    title: opts.title || "",
    onClick: opts.onClick || null,
  });
  chip.appendChild(el("span", { class: "pal-chip-label" }, opts.label));
  if (opts.sub) chip.appendChild(el("span", { class: "pal-chip-sub" }, opts.sub));
  chip.appendChild(el("span", { class: "spacer", style: "flex:1" }));
  (opts.actions || []).forEach((a) => chip.appendChild(a));
  return chip;
}

// Kleiner Icon-Knopf innerhalb eines Chips (Bearbeiten). ``stopPropagation``
// verhindert, dass der Klick den Chip selbst (Herkunft-Fokus) ausloest.
function chipAction(symbol, title, onClick) {
  return el("button", {
    class: "pal-chip-act", title,
    onClick: (e) => { e.stopPropagation(); onClick(); },
  }, symbol);
}

// Rechte Bindungs-Palette mit Tabs (Datenelemente | Ressourcen). Zeigt oben den
// aktuellen Bindungsziel-Schritt und bindet per Klick auf ⊕. Anlegen/Bearbeiten
// laeuft ueber dieselben Dialoge wie die Daten-/Ressourcensicht.
function bindingPalette(schema, draft) {
  const target = bindTarget();
  const tab = state.paletteTab === "res" ? "res" : "data";
  const selNode = state.selectedNode ? schema.nodes[state.selectedNode] : null;
  const subText = !draft ? "nur Ansicht (Schema freigegeben)"
    : target ? "Ziel: „" + nodeCaption(schema.nodes[target]) + "“"
      : selNode ? "gewählter Knoten ist kein Aktivitätsschritt"
        : "zuerst links einen Schritt wählen";
  const tabBtn = (id, label) => el("button", {
    class: "pal-tab" + (tab === id ? " active" : ""),
    "data-tour": "model.tab." + id,
    onClick: () => { state.paletteTab = id; render(); },
  }, label);
  const head = el("div", { class: "panel-h" },
    el("h2", null, "Binden"),
    el("span", { class: "sub" }, subText),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small ghost", title: "Vollansicht öffnen",
      onClick: () => focusBindingView(tab === "res" ? "org" : "data", null) }, "Vollansicht"));
  const tabs = el("div", { class: "pal-tabs" }, tabBtn("data", "Datenelemente"), tabBtn("res", "Ressourcen"));
  const body = el("div", { class: "panel-b pal-body" },
    tab === "res" ? resourcePaletteTab(schema, draft, target) : dataPaletteTab(schema, draft, target));
  // data-tour: Die Tour muss die GANZE Palette freilassen koennen -- der Tab
  // allein reicht nicht, die Bindung passiert ueber das ⊕ im Koerper darunter.
  return el("div", { class: "panel pal-panel", "data-tour": "model.palette" }, head, tabs, body);
}

// Aktueller Bindungsziel-Knoten: der gewaehlte Schritt, sofern Entwurf UND eine
// ACTIVITY (nur daran lassen sich Daten-/Bearbeiterbindungen setzen).
function bindTarget() {
  if (!isDraft(state.schema) || !state.selectedNode) return null;
  const n = state.schema.nodes[state.selectedNode];
  return n && n.type === NODE_TYPE.ACTIVITY ? n.id : null;
}

// "An Schritt binden"-Knopf (⊕). Deaktiviert, solange kein Aktivitätsschritt
// gewaehlt ist; ein Klick erklaert dann, was zu tun ist.
function bindButton(enabled, onBind) {
  return el("button", {
    class: "pal-bind" + (enabled ? "" : " disabled"),
    title: enabled ? "An gewählten Schritt binden" : "Zuerst links einen Schritt (Aktivität) wählen",
    onClick: (e) => {
      e.stopPropagation();
      if (enabled) onBind();
      else toast("info", "Zuerst links einen Schritt (Aktivität) wählen");
    },
  }, "⊕");
}

// Tab „Datenelemente": Liste mit Typ/Quelle. ⊕ bindet an den gewaehlten Schritt
// (Richtung/Pflicht im Dialog), ✎ bearbeitet, Klick hebt die Herkunft hervor.
function dataPaletteTab(schema, draft, target) {
  const elems = Object.values(schema.data_elements || {});
  const box = el("div");
  box.appendChild(el("div", { class: "pal-group-h" },
    el("span", null, "Datenelemente"),
    el("button", { class: "btn small", onClick: addDataElement, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Datenelement")));
  if (!elems.length) {
    box.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Noch keine Datenelemente – mit „+ Datenelement“ eines mit sprechendem Namen anlegen."));
    return box;
  }
  const wrap = el("div", { class: "pal-chips" });
  elems.forEach((d) => {
    const actions = [];
    if (draft) actions.push(bindButton(!!target, () => dropDataElementOnNode(target, { element_id: d.id, name: d.name })));
    if (draft) actions.push(chipAction("✎", "Datenelement bearbeiten", () => editDataElement(d)));
    wrap.appendChild(paletteChip({
      label: d.name,
      sub: typeName(d.data_type) + (d.source === "EXTERNAL" ? " · extern" : ""),
      title: `${d.name} (${typeName(d.data_type)}) – klicken zeigt die Herkunft im Graph`,
      active: state.dataElemFocus === d.id,
      onClick: () => { state.dataElemFocus = state.dataElemFocus === d.id ? null : d.id; render(); },
      actions,
    }));
  });
  box.appendChild(wrap);
  return box;
}

// Tab „Ressourcen": Rollen, Abteilungen und Agenten mit ⊕ (Bearbeiterzuordnung
// an den gewaehlten Schritt). Die BZR kann an eine Rolle/Abteilung oder – fuer
// die explizite Auswahl – an einen konkreten Agenten binden.
function resourcePaletteTab(schema, draft, target) {
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };
  const orgEditable = draft || !!schema.org_model_id;
  const box = el("div");
  const group = (title, addBtn, chipsWrap) => el("div", { class: "pal-group" },
    el("div", { class: "pal-group-h" }, el("span", null, title), addBtn || null), chipsWrap);

  const roles = Object.values(org.roles || {});
  const roleChips = el("div", { class: "pal-chips" });
  if (!roles.length) roleChips.appendChild(el("span", { class: "muted", style: "font-size:12px" }, "keine"));
  roles.forEach((r) => roleChips.appendChild(paletteChip({
    label: r.name, sub: "Rolle", title: `Rolle ${r.name}`,
    actions: draft ? [bindButton(!!target, () => dropResourceOnNode(target, { rkind: "ROLE", ref: r.id, name: r.name }))] : [],
  })));
  const addRoleBtn = el("button", { class: "btn small", disabled: !orgEditable, onClick: addRole }, "+ Rolle");

  const units = Object.values(org.org_units || {});
  const unitChips = el("div", { class: "pal-chips" });
  if (!units.length) unitChips.appendChild(el("span", { class: "muted", style: "font-size:12px" }, "keine"));
  units.forEach((u) => {
    const actions = [];
    if (draft) actions.push(bindButton(!!target, () => dropResourceOnNode(target, { rkind: "ORG_UNIT", ref: u.id, name: u.name })));
    if (orgEditable) actions.push(chipAction("✎", "Vorgesetzten der Abteilung setzen", () => editManager(u)));
    unitChips.appendChild(paletteChip({ label: u.name, sub: "Abteilung", title: `Abteilung ${u.name}`, actions }));
  });
  const addUnitBtn = el("button", { class: "btn small", disabled: !orgEditable, onClick: () => addChildOrgUnit(null) }, "+ Abteilung");

  const agents = Object.values(org.agents || {});
  const agentChips = el("div", { class: "pal-chips" });
  if (!agents.length) agentChips.appendChild(el("span", { class: "muted", style: "font-size:12px" }, "keine"));
  agents.forEach((a) => {
    const roleNames = (a.role_ids || []).map((id) => ((org.roles || {})[id] || {}).name).filter(Boolean).join(", ");
    const actions = [];
    if (draft) actions.push(bindButton(!!target, () => dropResourceOnNode(target, { rkind: "AGENT", ref: a.id, name: a.name })));
    if (orgEditable) actions.push(chipAction("✎", "Agent bearbeiten", () => editAgent(a)));
    agentChips.appendChild(paletteChip({
      label: a.name, sub: roleNames || "ohne Rolle", title: `Agent ${a.name}${roleNames ? " – " + roleNames : ""}`,
      actions,
    }));
  });
  const addAgentBtn = el("button", { class: "btn small", disabled: !orgEditable || !roles.length, onClick: addAgent }, "+ Agent");

  box.appendChild(group("Rollen", addRoleBtn, roleChips));
  box.appendChild(group("Abteilungen", addUnitBtn, unitChips));
  box.appendChild(group("Agenten", addAgentBtn, agentChips));
  return box;
}

// --------------------------------------------------------------------------
// Modellieren im Kontrollfluss: die Schritt-Karte
//
// Alles, was an einem Schritt getan werden kann, steht in EINER Karte, die zum
// gewaehlten Schritt gehoert und mit ihm verschwindet. Sie traegt -- wie die
// klassische Sicht -- keine Korrektheitslogik: jeder Knopf ruft dieselbe
// Funktion und damit denselben Endpunkt wie zuvor, der Kern validiert vor dem
// Commit.
// --------------------------------------------------------------------------

// Abschnitte, die ohne eigene Wahl aufgeklappt sind: das, was der Kern fuer die
// Freigabe braucht. Alles Weitere (Maske, Zeit, Mail, Klassifikation) bleibt
// eingeklappt, bis es gebraucht wird.
const CARD_DEFAULT_OPEN = ["findings", "flow", "data", "staff", "service"];

/**
 * Liest die aufgeklappten Abschnitte (localStorage), mit robustem Rueckfall.
 *
 * @returns {string[]} Ids der aufgeklappten Abschnitte.
 */
function readCardOpen() {
  if (Array.isArray(state.cardOpen)) return state.cardOpen;
  try {
    const raw = JSON.parse(localStorage.getItem("cardOpen") || "null");
    state.cardOpen = Array.isArray(raw) ? raw : CARD_DEFAULT_OPEN.slice();
  } catch (_e) {
    state.cardOpen = CARD_DEFAULT_OPEN.slice();
  }
  return state.cardOpen;
}

/** Ist der Abschnitt ``id`` aufgeklappt? */
function cardSecOpen(id) { return readCardOpen().includes(id); }

/** Merkt die aufgeklappten Abschnitte im Browser. */
function persistCardOpen() {
  try { localStorage.setItem("cardOpen", JSON.stringify(readCardOpen())); } catch (_e) { /* Privatmodus */ }
}

/** Klappt einen Abschnitt auf/zu und merkt die Wahl. */
function toggleCardSec(id) {
  const open = readCardOpen();
  state.cardOpen = open.includes(id) ? open.filter((x) => x !== id) : open.concat([id]);
  persistCardOpen();
  render();
}

/**
 * Waehlt einen Schritt und oeffnet gezielt einen Abschnitt seiner Karte.
 *
 * Einstiegspunkt fuer die Knoten-Badges, den Befund-Marker und den Schnellring:
 * Statt in eine andere Sicht zu wechseln, springt die Bedienung an genau die
 * Stelle der Karte, die gemeint ist.
 *
 * @param {string|null} nodeId Zu waehlender Knoten (null = Auswahl behalten).
 * @param {string} sec Abschnitts-Id (siehe CARD_DEFAULT_OPEN) oder "name".
 */
function openCardSection(nodeId, sec) {
  if (nodeId) state.selectedNode = nodeId;
  if (sec && sec !== "name" && !cardSecOpen(sec)) {
    state.cardOpen = readCardOpen().concat([sec]);
    persistCardOpen();
  }
  state.cardFocusSection = sec;
  render();
}

/**
 * Aktion des Schnellrings am Knoten (siehe renderNodeRing).
 *
 * Jede Aktion fuehrt auf einen bereits bestehenden Weg -- „danach einfuegen"
 * ist derselbe Dialog wie das „+" auf der Kante, nur von einem anderen
 * Ausloeser.
 *
 * @param {string} nodeId Der Knoten am Ring.
 * @param {string} key Aktions-Schluessel aus nodeRingActions.
 */
function nodeRingAction(nodeId, key) {
  if (key === "insert") { openInsertModal(nodeId); return; }
  if (key === "rename") { openCardSection(nodeId, "name"); return; }
  if (key === "bind") { openCardSection(nodeId, "data"); return; }
  if (key === "delete") { deleteNode(nodeId); return; }
  if (key === "empty-branch") { removeEmptyBranch(nodeId); }
}

// Pfeiltaste -> Himmelsrichtung fuer die Tastaturnavigation im Kontrollfluss.
const ARROW_DIRS = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };

/**
 * Nachbarknoten in einer Himmelsrichtung bestimmen -- die Grundlage der
 * Tastaturnavigation im Kontrollfluss (Stufe U4).
 *
 * Rein rechnend und layout-agnostisch: gearbeitet wird auf den Kastenmitten,
 * die das Layout liefert, nicht auf der Kantenliste. Dadurch funktioniert die
 * Navigation auch im gestapelten Rueckfall-Layout und erreicht auch Knoten, die
 * zwar sichtbar nebeneinander liegen, aber nicht direkt verbunden sind -- etwa
 * die Geschwister-Aeste einer Verzweigung, um die es bei ``↑``/``↓`` gerade
 * geht.
 *
 * Vorgehen: Kandidaten sind alle Knoten in der gewuenschten Richtung. Gewaehlt
 * wird zuerst nach dem Abstand auf der Bewegungsachse (das Spine-Layout legt
 * Knoten auf ein diskretes Raster aus Spalten und Bahnen, „naechste Spalte"
 * bzw. „naechste Bahn" ist also wohldefiniert) und erst innerhalb dieses
 * Abstands nach dem geringsten Versatz auf der Querachse. ``→`` folgt damit dem
 * Ablauf und steigt an einem Split in den Ast ab, statt den ganzen Block zu
 * ueberspringen.
 *
 * ``↑``/``↓`` bekommen zusaetzlich eine Schranke: es zaehlen nur Knoten, die
 * sich mit dem aktuellen **waagerecht ueberdecken**. Die Taste wechselt so die
 * Bahn an Ort und Stelle (Geschwister-Ast), und ohne Nachbarbahn passiert
 * nichts -- ohne diese Schranke landete man vom Spine aus in einem Zweig weit
 * hinter oder vor der aktuellen Stelle, weil die naechste Bahn nach oben dort
 * ueberall verlaeuft.
 *
 * @param {object} layout Ergebnis von layoutSchema (``{pos: {id: {x,y,w,h}}}``).
 * @param {string|null} fromId Aktuell gewaehlter Knoten. Ohne Auswahl (oder bei
 *   einer Auswahl, die es nicht mehr gibt) liefert jede Richtung den
 *   Einstiegsknoten links oben -- in einem wohlgeformten Modell der Start.
 * @param {"left"|"right"|"up"|"down"} dir Bewegungsrichtung.
 * @returns {string|null} Id des Nachbarn, oder null, wenn es in der Richtung
 *   keinen gibt (dann bleibt die Auswahl, wo sie ist).
 */
function graphNeighbor(layout, fromId, dir) {
  const pos = (layout && layout.pos) || {};
  const ids = Object.keys(pos);
  if (!ids.length) return null;
  const cx = (id) => pos[id].x + pos[id].w / 2;
  const cy = (id) => pos[id].y + pos[id].h / 2;
  if (!fromId || !pos[fromId]) {
    return ids.slice().sort((a, b) => (cx(a) - cx(b)) || (cy(a) - cy(b)))[0];
  }
  const vertical = dir === "up" || dir === "down";
  const forward = dir === "right" || dir === "down" ? 1 : -1;
  const main = vertical ? cy : cx;    // Achse, auf der bewegt wird
  const cross = vertical ? cx : cy;   // Achse, die den Gleichstand bricht
  // Knoten derselben Spalte/Bahn liegen exakt gleich; EPS faengt nur
  // Rundungsreste ab und haelt „gleiche Spalte" von „naechste Spalte" getrennt.
  const EPS = 1;
  const base = main(fromId), baseCross = cross(fromId);
  let best = null;
  ids.forEach((id) => {
    if (id === fromId) return;
    const step = (main(id) - base) * forward;
    if (step <= EPS) return;                       // liegt nicht in Bewegungsrichtung
    const off = Math.abs(cross(id) - baseCross);
    // Bahnwechsel nur senkrecht ueber-/untereinander (siehe oben).
    if (vertical && off >= (pos[fromId].w + pos[id].w) / 2 - EPS) return;
    if (!best || step < best.step - EPS || (step - best.step <= EPS && off < best.off)) {
      best = { id, step, off };
    }
  });
  return best ? best.id : null;
}

/**
 * Auswahl im Kontrollfluss per Tastatur bewegen (Stufe U4).
 *
 * Gemeinsame Operation **beider** Modellier-Oberflaechen (beide gleichwertig): sie
 * setzt nur ``state.selectedNode``; die jeweilige Sicht zeichnet sich neu und
 * rueckt den Knoten ueber ``centerCanvasOnNode`` wieder ins Bild -- inklusive
 * des Kartenrands, damit die Schritt-Karte den frisch gewaehlten Knoten nicht
 * verdeckt.
 *
 * @param {"left"|"right"|"up"|"down"} dir Bewegungsrichtung.
 * @returns {boolean} true, wenn sich die Auswahl geaendert hat (dann darf der
 *   Aufrufer die Taste als verbraucht behandeln und das Scrollen unterdruecken).
 */
function moveSelection(dir) {
  const schema = state.schema;
  if (!schema || !Object.keys(schema.nodes || {}).length) return false;
  const next = graphNeighbor(layoutSchema(schema), state.selectedNode, dir);
  if (!next || next === state.selectedNode) return false;
  state.selectedNode = next;
  state.cardFocusSection = null;   // neuer Schritt -> Karte oben beginnen
  render();
  return true;
}

/**
 * Fokus aus dem Kontrollfluss in die Schritt-Karte holen (Enter).
 *
 * Bevorzugt das Bezeichnungsfeld (der haeufigste naechste Handgriff), sonst das
 * erste Bedienelement der Karte. Ohne Karte -- klassische Sicht, keine Auswahl
 * -- passiert nichts, die Taste bleibt dann beim Browser.
 *
 * @returns {boolean} true, wenn ein Element den Fokus bekommen hat.
 */
function focusStepCard() {
  // Karte (Karten-Sicht) bzw. Knoten-Inspektor (klassische Sicht): Enter fuehrt
  // in beiden Oberflaechen in das Bearbeitungsfeld des gewaehlten Schritts.
  const card = document.querySelector(".step-card") || document.querySelector(".node-inspector");
  if (!card) return false;
  const name = byId("card-name-input") || byId("insp-name-input");
  if (name && name.focus) { name.focus(); if (name.select) name.select(); return true; }
  const first = card.querySelector("button, input, select, textarea, a[href]");
  if (first && first.focus) { first.focus(); return true; }
  return false;
}

/**
 * Ob die Taste gerade einem Eingabefeld gehoert.
 *
 * Trennt Tastenwege der Oberflaeche vom Tippen: waehrend in einem Feld
 * geschrieben wird, darf weder Escape die Karte wegraeumen noch eine Pfeiltaste
 * die Schreibmarke stehlen.
 *
 * @returns {boolean} true, wenn der Fokus in einem Text-/Auswahlfeld steht.
 */
function isTypingTarget() {
  const a = document.activeElement;
  if (!a) return false;
  if (a.isContentEditable) return true;
  const tag = a.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

/**
 * Sichert Fokus und Schreibmarke des Bezeichnungsfeldes vor einem Neuaufbau.
 *
 * @returns {{nodeId: string, start: number, end: number}|null} Merker oder null.
 */
function captureCardNameFocus() {
  const inp = byId("card-name-input");
  if (!inp || document.activeElement !== inp) return null;
  return {
    nodeId: state.selectedNode,
    start: inp.selectionStart != null ? inp.selectionStart : inp.value.length,
    end: inp.selectionEnd != null ? inp.selectionEnd : inp.value.length,
  };
}

/**
 * Stellt nach dem Neuaufbau Fokus/Schreibmarke wieder her und rollt einen
 * gezielt geoeffneten Abschnitt in den sichtbaren Bereich der Karte.
 *
 * @param {HTMLElement} content Der neu aufgebaute Inhaltsbereich.
 * @param {{nodeId: string, start: number, end: number}|null} nameFocus Merker.
 */
function applyCardFocus(content, nameFocus) {
  const wanted = state.cardFocusSection;
  state.cardFocusSection = null;
  const focusName = (nameFocus && nameFocus.nodeId === state.selectedNode) || wanted === "name";
  if (focusName) {
    const inp = byId("card-name-input");
    if (inp) {
      inp.focus();
      if (nameFocus && nameFocus.nodeId === state.selectedNode) {
        try { inp.setSelectionRange(nameFocus.start, nameFocus.end); } catch (_e) { /* kein Textfeld */ }
      } else {
        inp.select();
      }
    }
  }
  if (wanted && wanted !== "name") {
    const sec = content.querySelector('.card-sec[data-sec="' + wanted + '"]');
    if (sec && sec.scrollIntoView) requestAnimationFrame(() => sec.scrollIntoView({ block: "nearest" }));
  }
}

/**
 * Ein aufklappbarer Abschnitt der Schritt-Karte.
 *
 * @param {string} id Abschnitts-Id (Merker fuer auf-/zugeklappt).
 * @param {string} title Ueberschrift.
 * @param {string|null} summary Kurzstand rechts in der Kopfzeile (z. B. "2").
 * @param {function(HTMLElement): void} fill Baut den Koerper (nur wenn offen).
 * @param {string|null} anchor Optionaler ``data-tour``-Anker fuer die Tour.
 * @returns {HTMLElement} Der Abschnitt.
 */
function cardSection(id, title, summary, fill, anchor) {
  const open = cardSecOpen(id);
  const head = el("button", {
    class: "card-sec-h" + (open ? " open" : ""),
    type: "button",
    onClick: () => toggleCardSec(id),
    title: open ? "Abschnitt zuklappen" : "Abschnitt aufklappen",
  },
    el("span", { class: "card-sec-caret" }, open ? "▾" : "▸"),
    el("span", { class: "card-sec-t" }, title),
    el("span", { class: "spacer", style: "flex:1" }),
    summary ? el("span", { class: "card-sec-sum" }, summary) : null);
  if (anchor) head.setAttribute("data-tour", anchor);
  const sec = el("section", { class: "card-sec", "data-sec": id }, head);
  if (open) {
    const body = el("div", { class: "card-sec-b" });
    fill(body);
    sec.appendChild(body);
  }
  return sec;
}

/**
 * Freigabe-Ampel eines Schritts -- eine **Anzeige**, keine Pruefung.
 *
 * Rot = der Kern beanstandet diesen Knoten, gelb = keine Beanstandung, aber
 * etwas fuer die Freigabe Noetiges fehlt noch, gruen = aus Sicht der Karte
 * vollstaendig. Verbindlich ist ausschliesslich der Kern; die gelbe Stufe ist
 * bewusst nur ein Hinweis und blockiert nie eine Aktion.
 *
 * @param {object} schema Das Schema.
 * @param {object} node Der Knoten.
 * @param {Array} findings Befunde dieses Knotens.
 * @returns {{cls: string, text: string}|null} Ampel oder null (kein Arbeitsschritt).
 */
function stepReadiness(schema, node, findings) {
  if (node.type !== NODE_TYPE.ACTIVITY && node.type !== NODE_TYPE.SUBPROCESS) return null;
  if (findings.length) return { cls: "red", text: countLabel(findings.length, "Befund", "Befunde") + " des Kerns an diesem Schritt" };
  const missing = [];
  if (node.type === NODE_TYPE.ACTIVITY) {
    const sb = (schema.service_bindings || {})[node.id];
    if (!sb) missing.push("Dienst");
    // Ein automatischer Schritt braucht keinen Bearbeiter.
    if (!(schema.staff_rules || {})[node.id] && !(sb && sb.automatic)) missing.push("Bearbeiter");
  } else if (!(schema.sub_process_bindings || {})[node.id]) {
    missing.push("Submodell");
  }
  return missing.length
    ? { cls: "amber", text: "Für die Freigabe fehlt noch: " + missing.join(", ") }
    : { cls: "green", text: "Aus Sicht dieser Karte vollständig" };
}

/**
 * Die Schritt-Karte des gewaehlten Knotens (Overlay ueber dem Kontrollfluss).
 *
 * @param {object} schema Das aktuelle Schema.
 * @param {boolean} draft Ob bearbeitet werden darf (ENTWURF).
 * @returns {HTMLElement|null} Die Karte, oder null ohne Auswahl.
 */
function stepCard(schema, draft) {
  const node = state.selectedNode ? schema.nodes[state.selectedNode] : null;
  if (!node) return null;
  const findings = findingsByNode()[node.id] || [];
  const card = el("aside", {
    class: "step-card",
    "data-tour": "model.palette",
    "aria-label": "Schritt " + nodeCaption(node),
  });
  card.appendChild(stepCardHead(schema, node, draft, findings));
  const body = el("div", { class: "step-card-b" });
  if (findings.length) {
    body.appendChild(cardSection("findings", "Befunde", String(findings.length), (b) => {
      findings.forEach((f) => b.appendChild(el("div", { class: "finding" },
        el("span", { class: "rule" }, f.rule), el("span", null, findingText(f, { withHint: true })))));
    }));
  }
  if (!draft) {
    body.appendChild(el("div", { class: "card-note" },
      "Schema ist freigegeben – zum Bearbeiten eine neue Revision anlegen (Knoten-IDs bleiben erhalten)."));
    body.appendChild(newRevisionAction());
  }
  const isStep = node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS;
  if (isStep) {
    if (draft) body.appendChild(cardSection("flow", "Ablauf", null, (b) => cardFlowSection(b, schema, node)));
    body.appendChild(cardSection("data", "Daten", cardDataSummary(schema, node),
      (b) => cardDataSection(b, schema, node, draft), "model.tab.data"));
    if (node.type === NODE_TYPE.ACTIVITY) {
      body.appendChild(cardSection("staff", "Bearbeiter", null,
        (b) => cardStaffSection(b, schema, node, draft), "model.tab.res"));
      body.appendChild(cardSection("service", "Dienst", null, (b) => cardServiceSection(b, schema, node, draft)));
      body.appendChild(cardSection("sync", "Synchronisation", null, (b) => syncBlock(b, schema, node, draft)));
      body.appendChild(cardSection("form", "Eingabemaske", null, (b) => cardFormSection(b, schema, node, draft)));
    } else {
      body.appendChild(cardSection("sub", "Submodell", null, (b) => cardSubprocessSection(b, schema, node, draft)));
    }
    body.appendChild(cardSection("time", "Zeit & Priorität", null, (b) => cardTimeSection(b, schema, node, draft)));
    if (node.type === NODE_TYPE.ACTIVITY) {
      body.appendChild(cardSection("mail", "Benachrichtigung", null, (b) => cardMailSection(b, schema, node, draft)));
    }
    body.appendChild(cardSection("class", "Klassifikation", null, (b) => cardValueClassSection(b, schema, node, draft)));
  } else if (SPLIT_TYPES.has(node.type)) {
    body.appendChild(cardSection("branches", "Verzweigung", null, (b) => cardBranchSection(b, schema, node, draft)));
  } else if (LOOP_TYPES.has(node.type)) {
    body.appendChild(cardSection("loop", "Schleife", null,
      (b) => b.appendChild(loopNodePanel(schema, node, draft))));
  } else {
    body.appendChild(el("div", { class: "card-note" },
      node.type === NODE_TYPE.AND_JOIN || node.type === NODE_TYPE.XOR_JOIN
        ? "Join-Knoten werden über ihren öffnenden Split entfernt."
        : "Start und Ende sind fester Bestandteil des Modells."));
  }
  card.appendChild(body);
  return card;
}

/** Kopf der Schritt-Karte: Typ, Bezeichnung (bearbeitbar), Ampel, Schliessen. */
function stepCardHead(schema, node, draft, findings) {
  const head = el("div", { class: "step-card-h" });
  head.appendChild(el("span", { class: "pill pill-gray", title: node.type }, nodeTypeLabel(node.type)));
  const renamable = draft && (node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS);
  if (renamable) {
    const input = el("input", { type: "text", id: "card-name-input", value: node.label || "",
      title: "Bezeichnung – Enter übernimmt" });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") renameNode(node.id, input.value); });
    head.appendChild(input);
    head.appendChild(el("button", { class: "btn small", title: "Bezeichnung übernehmen",
      onClick: () => renameNode(node.id, input.value) }, "✓"));
  } else {
    head.appendChild(el("strong", { class: "step-card-name" }, nodeCaption(node)));
  }
  const ready = stepReadiness(schema, node, findings);
  if (ready) head.appendChild(el("span", { class: "step-dot step-dot-" + ready.cls, title: ready.text }));
  head.appendChild(el("button", { class: "step-card-x", title: "Auswahl aufheben (Esc)",
    onClick: () => { state.selectedNode = null; render(); } }, "✕"));
  return head;
}

/** Abschnitt „Ablauf": Schritt danach einfuegen, entfernen, umwandeln. */
function cardFlowSection(body, schema, node) {
  body.appendChild(el("div", { class: "card-hint" },
    "Neue Schritte entstehen immer relativ zu einem bestehenden – so kann kein loses Ende entstehen."));
  const movable = node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS;
  const row = el("div", { class: "row", style: "gap:8px" },
    el("button", { class: "btn small primary", onClick: () => openInsertModal(node.id) }, "+ Schritt danach"),
    movable ? el("button", { class: "btn small", title: "Schritt an eine andere Stelle des Ablaufs verschieben – alle Bindungen bleiben erhalten",
      onClick: () => moveNodeDialog(node.id) }, "Verschieben…") : null,
    el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Entfernen"));
  body.appendChild(row);
  if (node.type === NODE_TYPE.ACTIVITY) {
    body.appendChild(el("div", { class: "card-hint", style: "margin-top:10px" },
      "Wiederverwendung: diesen Schritt durch ein freigegebenes Submodell aus der Bibliothek ersetzen – inkl. Datenübergabe."));
    body.appendChild(el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "convert") },
      "In Subprozess umwandeln"));
  }
}

/** Kurzstand des Daten-Abschnitts (Anzahl Bindungen). */
function cardDataSummary(schema, node) {
  const n = (schema.data_accesses || []).filter((a) => a.node_id === node.id).length;
  return n ? String(n) : null;
}

/** Abschnitt „Daten": bestehende Bindungen loesen, neue direkt hier binden. */
function cardDataSection(body, schema, node, draft) {
  const accesses = (schema.data_accesses || []).filter((a) => a.node_id === node.id);
  if (accesses.length) {
    const list = el("div", { class: "insp-binds" });
    accesses.forEach((a) => {
      const e = schema.data_elements[a.element_id];
      const name = e ? e.name : a.element_id;
      list.appendChild(el("div", { class: "insp-bind" },
        el("button", { class: "insp-bind-name link-like", title: "Herkunft dieses Elements im Kontrollfluss zeigen",
          onClick: () => { state.dataElemFocus = state.dataElemFocus === a.element_id ? null : a.element_id; render(); } }, name),
        el("span", { class: "insp-bind-mode mode-" + a.mode, title: a.mode }, accessModeLabel(a.mode)),
        a.mandatory ? null : el("span", { class: "muted", style: "font-size:11px" }, "optional"),
        el("span", { class: "spacer", style: "flex:1" }),
        draft
          ? el("button", { class: "insp-bind-del", title: "Bindung lösen",
              onClick: () => removeDataAccess(node.id, a.element_id, a.mode, name) }, "✕")
          : null));
    });
    body.appendChild(list);
  } else {
    body.appendChild(el("div", { class: "card-hint" },
      "Dieser Schritt liest und schreibt bisher nichts. Gebunden wird hier – Richtung und Pflicht legst du im Dialog fest."));
  }
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" });
  if (draft) {
    row.appendChild(el("button", { class: "btn small primary", onClick: () => bindDataDialog(node.id) },
      "⊕ Datenelement binden"));
  }
  row.appendChild(el("button", { class: "btn small ghost", title: "Alle Datenbindungen des Modells in der Datensicht",
    onClick: () => focusBindingView("data", node.id) }, "Vollansicht"));
  body.appendChild(row);
}

/** Abschnitt „Bearbeiter": die BZR des Schritts (Z1–Z4). */
function cardStaffSection(body, schema, node, draft) {
  const rule = (schema.staff_rules || {})[node.id];
  const sb = (schema.service_bindings || {})[node.id];
  body.appendChild(el("div", { class: rule ? "" : "card-hint" },
    rule
      ? describeRule(rule)
      : sb && sb.automatic
        ? "Automatischer Schritt – er wird ohne Bearbeiter ausgeführt."
        : "Noch niemandem zugeordnet. Ohne Zuordnung landet die Aufgabe in keiner Arbeitsliste."));
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" });
  if (draft) {
    row.appendChild(el("button", { class: "btn small primary", onClick: () => bindStaffDialog(node.id) },
      rule ? "⊕ Zuordnung ändern" : "⊕ Bearbeiter zuordnen"));
    if (rule) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeStaffRule(node.id) }, "Entfernen"));
  }
  row.appendChild(el("button", { class: "btn small ghost", title: "Organisationsmodell in der Ressourcensicht bearbeiten",
    onClick: () => focusBindingView("org", node.id) }, "Organisation"));
  body.appendChild(row);
}

/** Abschnitt „Dienst" (A1–A3): was der Schritt ausfuehrt. */
/**
 * Hinweis fuer Schritte ohne Dienst -- geteilt von Schritt-Karte und klassischem
 * Inspektor, damit beide Oberflaechen dasselbe sagen. Ein Dienst ist optional:
 * die Freigabe (Stufe B) verlangt bei interaktiven Schritten nur eine
 * Bearbeiterzuordnung (B2); B1 wird bewusst NICHT erzwungen (siehe
 * validator.check_executable). Frueherer Text behauptete das Gegenteil
 * (bis 1.17.0).
 */
const SERVICE_OPTIONAL_HINT =
  "Kein Dienst – optional. Für die Freigabe braucht ein interaktiver Schritt nur eine Bearbeiterzuordnung (B2); einen Dienst braucht nur ein automatischer Schritt.";

function cardServiceSection(body, schema, node, draft) {
  const sb = (schema.service_bindings || {})[node.id];
  if (sb) {
    const tmpl = sb.template_id ? (schema.activity_templates || {})[sb.template_id] : null;
    body.appendChild(el("div", { style: "font-size:13px" },
      el("strong", null, sb.name), " · ", sb.automatic ? "automatisch" : "interaktiv",
      tmpl ? ` · Template „${tmpl.name}“` : ""));
    if (sb.automatic && sb.automation && sb.automation !== "MANUAL_NONE") {
      body.appendChild(el("div", { class: "muted", style: "font-size:11px" },
        "Automatik: " + (AUTOMATION_LABELS[sb.automation] || sb.automation)));
    }
  } else {
    body.appendChild(el("div", { class: "card-hint" },
      SERVICE_OPTIONAL_HINT));
  }
  if (!draft) return;
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small" + (sb ? "" : " primary"), onClick: () => assignServiceFor(node.id) },
      sb ? "Dienst ändern" : "Dienst zuweisen"));
  if (sb && sb.automatic) row.appendChild(el("button", { class: "btn small", onClick: () => editAutomation(node, sb) }, "Automatik…"));
  if (sb) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeService(node.id) }, "Entfernen"));
  body.appendChild(row);
}

/** Abschnitt „Eingabemaske" (U1–U2). */
function cardFormSection(body, schema, node, draft) {
  const form = schema.forms && schema.forms[node.id];
  body.appendChild(el("div", { class: form ? "" : "card-hint", style: "font-size:13px" },
    form
      ? `${countLabel(form.fields.length, "Feld", "Felder")}${form.title ? " – „" + form.title + "“" : ""}.`
      : "Noch keine Eingabemaske – Felder per Auswahl zusammenstellen."));
  if (!draft) return;
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small", onClick: () => openFormDesigner(node.id) },
      form ? "Maske bearbeiten" : "Eingabemaske gestalten"));
  if (form) row.appendChild(el("button", { class: "btn small danger", onClick: () => deleteForm(node.id) }, "Maske entfernen"));
  body.appendChild(row);
}

/** Abschnitt „Zeit & Priorität" (T1/T2 und die Arbeitslisten-Reihung). */
function cardTimeSection(body, schema, node, draft) {
  const tc = (schema.time_constraints || {})[node.id];
  const hasFrist = tc && tc.max_duration_seconds != null;
  body.appendChild(el("div", { class: "insp-h" }, "Frist (max. Dauer)"));
  body.appendChild(el("div", { class: hasFrist ? "" : "card-hint", style: "font-size:13px" },
    hasFrist ? formatDuration(tc.max_duration_seconds)
      : "keine – fließt bei gesetztem Prozess-Termin in die Terminprüfung (T2) ein."));
  if (tc && tc.target_lead_seconds != null) {
    body.appendChild(el("div", { style: "font-size:12px;margin-top:4px" },
      "Soll-Reaktionszeit ab Aktivierung: " + formatDuration(tc.target_lead_seconds)));
  }
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => setTimeConstraintFor(node.id, hasFrist ? tc : null) },
        hasFrist ? "Frist ändern" : "Frist setzen"),
      hasFrist ? el("button", { class: "btn small danger", onClick: () => removeTimeConstraint(node.id) }, "Entfernen") : null));
  }
  escalationBlock(body, schema, node, draft);
  const pr = (schema.node_priorities || {})[node.id];
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Priorität"));
  body.appendChild(el("div", { class: pr ? "" : "card-hint", style: "font-size:13px" },
    pr ? `Auswirkung ${IMPACT_LABELS[pr.impact] || pr.impact} · Dringlichkeit ${IMPACT_LABELS[pr.urgency] || pr.urgency}`
      : "normal (Standard)"));
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => setPriorityFor(node.id, pr) }, pr ? "Priorität ändern" : "Priorität setzen"),
      pr ? el("button", { class: "btn small danger", onClick: () => removePriority(node.id) }, "Entfernen") : null));
  }
}

/** Abschnitt „Benachrichtigung" (Regelgruppe N, opt-in je Aktivitaet). */
function cardMailSection(body, schema, node, draft) {
  const binding = (schema.mail_bindings || {})[node.id];
  const hasRule = !!(schema.staff_rules || {})[node.id];
  if (binding) {
    body.appendChild(el("div", { style: "font-size:13px" },
      MAIL_MODE_LABELS[binding.mode] || binding.mode,
      binding.mode === "TO_ELIGIBLE_AGENTS"
        ? (binding.include_deputies ? " · inkl. Vertreter" : " · ohne Vertreter") : ""));
    body.appendChild(el("div", { class: "muted", style: "font-size:11px;margin-top:2px" },
      "Betreff: " + (binding.subject || "–")));
  } else if (!hasRule) {
    // Abhaengigkeit erklaeren statt verstecken (N2) -- mit Sprungmarke auf den
    // Abschnitt, der das Fehlende beschafft.
    body.appendChild(el("div", { class: "card-hint" },
      "Erst einen Bearbeiter zuordnen – ohne Zuordnung gibt es keinen Empfänger (N2). ",
      el("a", { href: "#", onClick: (e) => { e.preventDefault(); openCardSection(node.id, "staff"); } },
        "Zum Abschnitt Bearbeiter")));
  } else {
    body.appendChild(el("div", { class: "card-hint" },
      "Aus – bei Aufgabeneingang wird keine Mail gesendet (Standard)."));
  }
  if (!draft) return;
  const row = el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small", disabled: !hasRule && !binding,
      onClick: () => editMailBinding(node.id, binding) }, binding ? "Mail ändern" : "Mail senden…"));
  if (binding) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeMailBinding(node.id) }, "Entfernen"));
  body.appendChild(row);
}

/** Abschnitt „Klassifikation": Wertklasse (rein beratend, E3). */
function cardValueClassSection(body, schema, node, draft) {
  const sel = el("select", null,
    el("option", { value: "" }, "– keine –"),
    ...Object.entries(VALUE_CLASS_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  sel.value = node.value_class || "";
  sel.disabled = !draft;
  sel.addEventListener("change", () => setValueClass(node.id, sel.value || null));
  body.appendChild(el("div", { class: "card-hint" },
    "Rein beratend – ohne Wirkung auf die Korrektheit oder die Ausführung."));
  body.appendChild(el("label", { class: "field", style: "font-size:12px;margin-top:6px" }, "Klassifikation", sel));
}

/** Abschnitt „Submodell" eines Subprozess-Knotens (H1–H4). */
function cardSubprocessSection(body, schema, node, draft) {
  const bnd = (schema.sub_process_bindings || {})[node.id];
  body.appendChild(el("div", { class: bnd ? "" : "card-hint", style: "font-size:13px" },
    bnd ? `Gebunden an Submodell „${bnd.target_schema_id}“ (v${bnd.target_version}).` : "Noch kein Submodell gebunden."));
  if (!draft) return;
  body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:8px" },
    el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "rebind") },
      "Zuordnung / Datenübergabe ändern")));
}

/** Abschnitt „Verzweigung" eines Splits (K7-Zellen, leerer Zweig, entfernen). */
function cardBranchSection(body, schema, node, draft) {
  const branches = controlEdges(schema).filter((e) => e.source === node.id);
  const list = el("div", { class: "insp-binds" });
  branches.forEach((e) => {
    const target = schema.nodes[e.target];
    const empty = target && (target.type === NODE_TYPE.XOR_JOIN || target.type === NODE_TYPE.AND_JOIN);
    list.appendChild(el("div", { class: "insp-bind" },
      el("span", { class: "insp-bind-name" }, empty ? "leerer Zweig" : nodeCaption(target)),
      e.condition ? el("span", { class: "muted", style: "font-size:11px" }, conditionCaption(e.condition)) : null));
  });
  if (branches.length) body.appendChild(list);
  if (!draft) return;
  const emptyJoin = emptyBranchJoin(schema, node.id);
  if (emptyJoin) {
    body.appendChild(el("div", { class: "card-hint", style: "margin-top:8px" },
      "Ein Zweig ist leer – in ihm fällt keine Aktivität an. Entfernen löst bei nur noch einem verbleibenden Zweig die ganze Verzweigung auf."));
    body.appendChild(el("button", { class: "btn small", onClick: () => removeEmptyBranch(node.id) }, "Leeren Zweig entfernen"));
  }
  if (node.type === NODE_TYPE.AND_SPLIT) {
    body.appendChild(el("div", { class: "card-hint", style: "margin-top:8px" },
      "Querschritt: ein neuer Schritt als eigener Parallelzweig, per Sync-Kanten nach/vor gewählten Schritten (K4)."));
    body.appendChild(el("button", { class: "btn small", onClick: () => insertBetweenDialog() }, "Querschritt einfügen…"));
  }
  body.appendChild(el("div", { class: "card-hint", style: "margin-top:8px" },
    "Entfernen löscht den gesamten Block (Split, Zweige und passenden Join)."));
  body.appendChild(el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Verzweigung entfernen"));
}

// --------------------------------------------------------------------------
// Binden direkt am Schritt: Auswahl + Optionen in EINEM Dialog
// --------------------------------------------------------------------------

/**
 * Alle Knoten, die im Kontrollfluss **vor** ``nodeId`` liegen (Rueckwaerts-BFS).
 *
 * Dient nur der Sortierung/Beschriftung im Bindungsdialog: ein Datenelement,
 * das ein vorgelagerter Schritt schreibt, ist der uebliche Lese-Kandidat.
 * Verbindlich bleibt der Kern (D1–D4) -- diese Funktion entscheidet nichts.
 *
 * @param {object} schema Das Schema.
 * @param {string} nodeId Zielknoten.
 * @returns {Set<string>} Ids aller Vorgaenger (transitiv).
 */
function ancestorsOf(schema, nodeId) {
  const inEdges = {};
  controlEdges(schema).forEach((e) => { (inEdges[e.target] = inEdges[e.target] || []).push(e.source); });
  const seen = new Set();
  const queue = (inEdges[nodeId] || []).slice();
  while (queue.length) {
    const id = queue.pop();
    if (seen.has(id)) continue;
    seen.add(id);
    (inEdges[id] || []).forEach((p) => { if (!seen.has(p)) queue.push(p); });
  }
  return seen;
}

/**
 * Auswahlliste mit Suchfeld -- gemeinsamer Baustein der Bindungsdialoge.
 *
 * @param {Array<{id: string, label: string, sub: string, group: string}>} items Einträge.
 * @param {function(string): void} onPick Rueckmeldung der Auswahl (Element-Id).
 * @returns {{node: HTMLElement, get: function(): string|null}} Steuerelement.
 */
function pickList(items, onPick, initial) {
  // ``initial`` waehlt einen Eintrag vor (z. B. das soeben angelegte Element).
  let chosen = initial && items.some((it) => it.id === initial) ? initial
    : items.length ? items[0].id : null;
  const list = el("div", { class: "pick-list" });
  const search = el("input", { type: "text", placeholder: "Suchen …" });
  function build() {
    clear(list);
    const q = search.value.trim().toLowerCase();
    let group = null;
    const hits = items.filter((it) => !q
      || it.label.toLowerCase().includes(q) || (it.sub || "").toLowerCase().includes(q));
    if (!hits.length) {
      list.appendChild(el("div", { class: "muted", style: "font-size:12px;padding:8px" }, "Nichts gefunden."));
      return;
    }
    hits.forEach((it) => {
      if (it.group && it.group !== group) {
        group = it.group;
        list.appendChild(el("div", { class: "pick-group" }, group));
      }
      const row = el("button", {
        class: "pick-row" + (it.id === chosen ? " active" : ""),
        type: "button",
        onClick: () => { chosen = it.id; build(); if (onPick) onPick(chosen); },
      },
        el("span", { class: "pick-label" }, it.label),
        it.sub ? el("span", { class: "pick-sub" }, it.sub) : null);
      list.appendChild(row);
    });
  }
  search.addEventListener("input", build);
  build();
  return {
    node: el("div", { class: "pick" }, search, list),
    get: () => chosen,
  };
}

/**
 * Legt eine Datenbindung im Kern an -- der **einzige** Aufruf von
 * ``POST /schemas/{id}/data-access`` im Client.
 *
 * Alle Bindungswege (Schritt-Karte, Palette der klassischen Sicht, Datensicht,
 * das Merkmal einer neuen Verzweigung) laufen hierueber; der Kern prueft D1-D4.
 * Ein Waechter (test_every_data_binding_goes_through_one_function) haelt das so,
 * damit kein Weg wieder still auf „immer Pflicht“ oder englische Modi faellt.
 *
 * @param {string} schemaId Schema
 * @param {string} nodeId Schritt
 * @param {string} elementId Datenelement
 * @param {"READ"|"WRITE"|"READ_WRITE"} mode Richtung
 * @param {boolean} mandatory Pflichtbindung
 * @returns {Promise<object>} das geaenderte Schema (Fehler als Ausnahme)
 */
function createDataAccess(schemaId, nodeId, elementId, mode, mandatory) {
  return api.post(`/schemas/${schemaId}/data-access`, {
    node_id: nodeId, element_id: elementId, mode, mandatory,
  });
}

/**
 * Bindet ein Datenelement an einen Schritt -- Auswahl, Richtung und Pflicht in
 * einem Dialog, direkt am gewaehlten Schritt.
 *
 * Der Kern prueft die Bindung vor dem Commit (D1–D4): ein Pflichtlesen ohne
 * vorherige Schreibquelle wird mit HTTP 422 abgewiesen, das Modell bleibt
 * unveraendert und der Befund erscheint als Meldung.
 *
 * Vorgaben: Die Richtung steht auf „Schreiben“, solange kein
 * Schritt davor das gewaehlte Element setzt -- „Lesen“ fuehrte dort sicher zu
 * D1. Ueber „Neues Datenelement anlegen“ kommt man mit dem neuen Element
 * vorgewaehlt hierher zurueck (``preselect``).
 *
 * @param {string} nodeId Zielschritt.
 * @param {string} [preselect] vorzuwaehlendes Datenelement
 */
function bindDataDialog(nodeId, preselect) {
  const schema = state.schema;
  const node = schema.nodes[nodeId];
  const before = ancestorsOf(schema, nodeId);
  const writtenBefore = new Set();
  (schema.data_accesses || []).forEach((a) => {
    if (before.has(a.node_id) && (a.mode === "WRITE" || a.mode === "READ_WRITE")) writtenBefore.add(a.element_id);
  });
  const elems = Object.values(schema.data_elements || {});
  const items = elems.map((d) => ({
    id: d.id,
    label: d.name,
    sub: typeName(d.data_type) + (d.source === "EXTERNAL" ? " · extern" : "") + (writtenBefore.has(d.id) ? " · davor gesetzt" : ""),
    group: writtenBefore.has(d.id) ? "Von einem vorgelagerten Schritt gesetzt" : "Übrige Datenelemente",
  })).sort((a, b) => (a.group === b.group ? a.label.localeCompare(b.label) : a.group < b.group ? -1 : 1));
  if (!items.length) {
    // Ohne Datenelement gibt es nichts zu binden -- direkt das Anlegen anbieten
    // und danach zurueck in den Bindungsdialog.
    addDataElement((id) => bindDataDialog(nodeId, id));
    return;
  }
  const modeSel = el("select", null,
    el("option", { value: "READ" }, "Lesen (liest den Wert)"),
    el("option", { value: "WRITE" }, "Schreiben (setzt den Wert)"),
    el("option", { value: "READ_WRITE" }, "Lesen und Schreiben"));
  const defaultMode = (id) => { modeSel.value = writtenBefore.has(id) ? "READ" : "WRITE"; };
  const picker = pickList(items, defaultMode, preselect);
  defaultMode(picker.get());
  const mandBox = el("input", { type: "checkbox" });
  mandBox.checked = true;
  openModal(`Datenelement an „${nodeCaption(node)}" binden`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Was dieser Schritt liest, muss vorher jemand geschrieben haben – sonst weist der Kern die Bindung ab (D1)."),
      picker.node,
      el("div", { class: "form-grid", style: "margin-top:12px" },
        el("label", { class: "field" }, "Richtung", modeSel),
        el("label", { class: "row", style: "gap:8px;align-items:center" }, mandBox, "Pflichtbindung")),
      el("div", { style: "margin-top:10px" },
        el("button", { class: "btn small ghost", type: "button",
          onClick: () => addDataElement((id) => bindDataDialog(nodeId, id)) },
          "＋ Neues Datenelement anlegen"))),
    async () => {
      const id = picker.get();
      if (!id) { toast("err", "Kein Datenelement gewählt"); return false; }
      const elem = schema.data_elements[id];
      try {
        await createDataAccess(state.schemaId, nodeId, id, modeSel.value, mandBox.checked);
        await refreshSchema(); render();
        toast("ok", "Datenbindung gesetzt", [`${elem ? elem.name : id} (${accessModeLabel(modeSel.value)})`]);
      } catch (err) { toastError(err); return false; }
    }, "Binden");
}

/**
 * Ordnet einem Schritt Bearbeiter zu (BZR) -- Rolle, Abteilung oder Agent,
 * ausgewaehlt direkt am Schritt.
 *
 * Die node-referenzierenden Arten (Bearbeiter/Vorgesetzte:r eines frueheren
 * Schritts, Z3) haben eine eigene Referenzauswahl und laufen deshalb weiter
 * ueber den Dialog ``addStaffRule``, der von hier aus erreichbar bleibt.
 *
 * @param {string} nodeId Zielschritt.
 */
function bindStaffDialog(nodeId) {
  const schema = state.schema;
  const node = schema.nodes[nodeId];
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };
  const current = (schema.staff_rules || {})[nodeId];
  const items = [];
  Object.values(org.roles || {}).forEach((r) =>
    items.push({ id: "ROLE:" + r.id, label: r.name, sub: "Rolle", group: "Rollen" }));
  Object.values(org.org_units || {}).forEach((u) =>
    items.push({ id: "ORG_UNIT:" + u.id, label: u.name, sub: "Abteilung", group: "Abteilungen" }));
  Object.values(org.agents || {}).forEach((a) => {
    const roleNames = (a.role_ids || []).map((id) => ((org.roles || {})[id] || {}).name).filter(Boolean).join(", ");
    items.push({ id: "AGENT:" + a.id, label: a.name, sub: roleNames || "ohne Rolle", group: "Agenten" });
  });
  if (!items.length) {
    toast("info", "Noch keine Rollen/Abteilungen/Agenten", ["Zuerst in der Ressourcensicht anlegen."]);
    focusBindingView("org", nodeId);
    return;
  }
  const recBox = el("input", { type: "checkbox" });
  const recField = el("label", { class: "row", style: "gap:8px;align-items:center" },
    recBox, "Abteilung und alle Bereiche darunter");
  const syncRec = () => {
    const id = picker.get() || "";
    recField.style.display = id.startsWith("ORG_UNIT:") ? "" : "none";
  };
  const picker = pickList(items, syncRec);
  syncRec();
  const others = otherStepsBox(schema, nodeId);
  openModal(`Bearbeiter für „${nodeCaption(node)}"`,
    el("div", null,
      current ? el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        `Ersetzt die bestehende Zuordnung: ${describeRule(current)}.`) : null,
      picker.node,
      el("div", { style: "margin-top:10px" }, recField),
      others.node,
      el("div", { style: "margin-top:10px" },
        el("button", { class: "btn small ghost", type: "button", onClick: () => addStaffRule(nodeId) },
          "Erweiterte Regel (UND/ODER/AUSSER, Bearbeiter eines früheren Schritts) …"))),
    async () => {
      const picked = picker.get();
      if (!picked) { toast("err", "Nichts gewählt"); return false; }
      const [kind, ref] = [picked.slice(0, picked.indexOf(":")), picked.slice(picked.indexOf(":") + 1)];
      const rule = { kind, ref };
      if (kind === "ORG_UNIT") rule.recursive = recBox.checked;
      return applyStaffRuleTo(nodeId, others.selected(), rule);
    }, "Zuordnen");
}

/**
 * Setzt eine Bearbeiterregel auf einen Schritt und optional auf weitere.
 *
 * Die **eine** Stelle, an der eine Zuordnung geschrieben wird -- beide Dialoge
 * (Schritt-Karte und Ressourcensicht) rufen sie auf. Beide koennen mehrere
 * Schritte auf einmal zuordnen -- wer die Zuordnung in der Ressourcensicht
 * sucht, findet dort dieselbe Moeglichkeit.
 *
 * Jede Zuordnung bleibt eine **eigene** Kern-Operation mit eigener Prüfung --
 * keine Sammel-Abkürzung. Scheitert eine, bleiben die anderen bestehen, und die
 * Meldung nennt, welche nicht ging.
 *
 * @param {string} nodeId der Schritt des Dialogs
 * @param {string[]} extra weitere Schritte aus „Auch weiteren Schritten zuordnen"
 * @param {object} rule die Bearbeiterregel (StaffRule)
 * @returns {Promise<boolean|undefined>} false, wenn schon der erste Schritt scheiterte
 *   (dann bleibt der Dialog offen)
 */
async function applyStaffRuleTo(nodeId, extra, rule) {
  try {
    await api.post(`/schemas/${state.schemaId}/staff-rule`, { node_id: nodeId, rule });
  } catch (err) { toastError(err); return false; }
  const failed = [];
  for (const other of extra) {
    try { await api.post(`/schemas/${state.schemaId}/staff-rule`, { node_id: other, rule }); }
    catch (err) { const d = describeError(err); failed.push(`${nodeLabelOf(other)}: ${d.lines.join(" ") || d.title}`); }
  }
  await refreshSchema(); render();
  const done = 1 + extra.length - failed.length;
  if (failed.length) toast("err", `${countLabel(done, "Schritt", "Schritte")} zugeordnet, ${failed.length} nicht`, failed);
  else toast("ok", done > 1 ? `${done} Schritten zugeordnet` : "Bearbeiter zugeordnet", [describeRule(rule)]);
}

/**
 * Auswahl „Auch diesen Schritten zuordnen“ im Bearbeiter-Dialog.
 *
 * Bietet die uebrigen interaktiven Schritte an (automatische tragen keine
 * Bearbeiterregel, Z4). Schritte ohne Bearbeiter stehen oben und lassen sich mit
 * einem Klick alle waehlen -- der haeufigste Fall vor einer Freigabe (B2).
 * Wird von ``bindStaffDialog`` genutzt, das beide Oberflaechen aufrufen.
 *
 * @param {object} schema das Schema
 * @param {string} nodeId der Schritt, fuer den der Dialog offen ist
 * @returns {{node: HTMLElement|null, selected: () => string[]}}
 */
function otherStepsBox(schema, nodeId) {
  const rules = schema.staff_rules || {};
  const bindings = schema.service_bindings || {};
  const candidates = activitiesOf(schema)
    .filter((n) => n.id !== nodeId && !(bindings[n.id] && bindings[n.id].automatic))
    .sort((a, b) => Number(Boolean(rules[a.id])) - Number(Boolean(rules[b.id])));
  if (!candidates.length) return { node: null, selected: () => [] };
  const boxes = candidates.map((n) => {
    const box = el("input", { type: "checkbox", value: n.id });
    return { box, row: el("label", { class: "check-row" }, box, " " + nodeCaption(n),
      rules[n.id] ? el("span", { class: "muted", style: "font-size:11px" }, ` (ersetzt: ${describeRule(rules[n.id])})`) : null) };
  });
  const unstaffed = boxes.filter((b) => !rules[b.box.value]);
  const node = el("details", { class: "multi-steps", style: "margin-top:10px" },
    el("summary", null, "Auch weiteren Schritten zuordnen …"),
    unstaffed.length
      ? el("button", { class: "btn small ghost", type: "button", style: "margin:6px 0",
          onClick: () => unstaffed.forEach((b) => { b.box.checked = true; }) },
          `Alle ohne Bearbeiter wählen (${unstaffed.length})`)
      : null,
    el("div", { class: "check-list" }, ...boxes.map((b) => b.row)));
  return { node, selected: () => boxes.filter((b) => b.box.checked).map((b) => b.box.value) };
}

// Bindet ein Datenelement an den gewaehlten Schritt (⊕ in der Palette der
// klassischen Sicht): derselbe Dialog wie an der Schritt-Karte, mit dem Element
// vorgewaehlt -- Richtung (Lesen/Schreiben) und Pflicht/optional inklusive.
function dropDataElementOnNode(nodeId, payload) {
  bindDataDialog(nodeId, payload.element_id);
}

// Bindet eine Rolle/Abteilung an den gewaehlten Schritt (⊕ in der Palette):
// setzt dessen Bearbeiterzuordnung (BZR). Eine bestehende Zuordnung wird ersetzt
// (der Kern prueft Z1-Z4; eine unerfuellbare Regel wird abgewiesen).
function dropResourceOnNode(nodeId, payload) {
  const node = state.schema.nodes[nodeId];
  const current = (state.schema.staff_rules || {})[nodeId];
  const recBox = el("input", { type: "checkbox" });
  const recField = el("label", { class: "row", style: "gap:8px;align-items:center" },
    recBox, "Abteilung und alle Bereiche darunter");
  const label = payload.rkind === "ROLE" ? "Rolle" : payload.rkind === "AGENT" ? "Agent" : "Abteilung";
  // Wie der Bearbeiter-Dialog der Schritt-Karte: weitere Schritte und die
  // erweiterte Regel ueber dieselben Funktionen (otherStepsBox, addStaffRule,
  // applyStaffRuleTo).
  const others = otherStepsBox(state.schema, nodeId);
  openModal(`Bearbeiter für „${nodeCaption(node)}"`,
    el("div", { class: "form-grid" },
      el("div", { class: "field" }, `${label}: `, el("strong", null, payload.name)),
      current ? el("div", { class: "muted", style: "font-size:12px" },
        `Ersetzt die bestehende Zuordnung: ${describeRule(current)}.`) : null,
      payload.rkind === "ORG_UNIT" ? recField : null,
      others.node,
      el("div", null,
        el("button", { class: "btn small ghost", type: "button", onClick: () => addStaffRule(nodeId) },
          "Erweiterte Regel (UND/ODER/AUSSER, Bearbeiter eines früheren Schritts) …"))),
    async () => {
      const rule = { kind: payload.rkind, ref: payload.ref };
      if (payload.rkind === "ORG_UNIT") rule.recursive = recBox.checked;
      return applyStaffRuleTo(nodeId, others.selected(), rule);
    }, "Zuordnen");
}

// Löst eine einzelne Datenbindung eines Schritts (Inspektor „✕"). Läuft über den
// Kern (DELETE /data-access): ist die Bindung noch nötig – etwa der einzige
// Schreiber hinter einem Pflichtlesen (D1) – weist der Kern mit 422 ab und die
// Bindung bleibt bestehen.
async function removeDataAccess(nodeId, elementId, mode, name) {
  await commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/data-access/${nodeId}/${elementId}?mode=${encodeURIComponent(mode)}`), "Datenbindung gelöst", [`${name} (${mode})`]);
}

// Entfernt die Bearbeiterzuordnung (BZR) eines Schritts (Inspektor „Entfernen").
// Läuft über den Kern (DELETE /staff-rule); ein Schritt ohne Bearbeiter ist im
// Entwurf zulässig (B2 greift erst bei der Freigabe).
async function removeStaffRule(nodeId) {
  await commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/staff-rule/${nodeId}`), "Bearbeiterzuordnung entfernt");
}

// --------------------------------------------------------------------------
// Knoten-Inspektor: weitere Standardaktivitäten am Schritt
// (Dienst/A1–A3, Frist/T1, Priorität/E8, Wertklasse/E3)
// --------------------------------------------------------------------------

const VALUE_CLASS_LABELS = {
  VALUE_ADDING: "Wertschöpfend", BUSINESS_NECESSARY: "Betrieblich nötig",
  NON_VALUE_ADDING: "Nicht wertschöpfend",
};
const IMPACT_LABELS = { LOW: "niedrig", MEDIUM: "mittel", HIGH: "hoch" };
const AUTOMATION_LABELS = { EXTERNAL_TASK: "External-Task", HTTP_PUSH: "HTTP-Push" };

// Dauer (Sekunden) menschenlesbar in der groebsten glatt teilenden Einheit.
function formatDuration(sec) {
  if (sec == null) return "–";
  if (sec % 86400 === 0) return countLabel(sec / 86400, "Tag", "Tage");
  if (sec % 3600 === 0) return sec / 3600 + " Std.";
  if (sec % 60 === 0) return sec / 60 + " Min.";
  return sec + " Sek.";
}

// Dauer-Eingabe (Zahl + Einheit). ``read()`` liefert Sekunden bzw. ``null``
// (leer/ungueltig -> je nach Aufrufer "keine Angabe"/"loeschen").
function durationControls(currentSec) {
  const units = [["60", "Minuten"], ["3600", "Stunden"], ["86400", "Tage"]];
  let val = "", unit = "3600";
  if (currentSec != null) {
    if (currentSec % 86400 === 0) { unit = "86400"; val = String(currentSec / 86400); }
    else if (currentSec % 3600 === 0) { unit = "3600"; val = String(currentSec / 3600); }
    else { unit = "60"; val = String(Math.round(currentSec / 60)); }
  }
  const num = el("input", { type: "number", min: "0", step: "any", value: val, placeholder: "z. B. 4" });
  const unitSel = el("select", null, ...units.map(([v, l]) => el("option", { value: v }, l)));
  unitSel.value = unit;
  const node = el("div", { class: "row", style: "gap:8px" },
    el("label", { class: "field", style: "flex:1" }, "Dauer", num),
    el("label", { class: "field", style: "flex:1" }, "Einheit", unitSel));
  const read = () => { const n = parseFloat(num.value); if (!(n > 0)) return null; return Math.round(n * Number(unitSel.value)); };
  return { node, read };
}

// Haengt die Abschnitte Dienst/Frist/Priorität/Wertklasse an den Inspektor-Body.
// Dienst nur fuer ACTIVITY (ein SUBPROCESS wird durch sein Submodell ausgefuehrt);
// Frist/Priorität/Wertklasse gelten fuer ACTIVITY und SUBPROCESS.
function nodePerformSections(body, schema, node) {
  const isActivity = node.type === NODE_TYPE.ACTIVITY;

  if (isActivity) {
    syncBlock(body, schema, node, true);
    const sb = (schema.service_bindings || {})[node.id];
    body.appendChild(el("div", { class: "hr" }));
    body.appendChild(el("div", { class: "insp-h" }, "Dienst"));
    if (sb) {
      const tmpl = sb.template_id ? (schema.activity_templates || {})[sb.template_id] : null;
      body.appendChild(el("div", { style: "font-size:12px" },
        el("strong", null, sb.name), " · ", sb.automatic ? "automatisch" : "interaktiv",
        tmpl ? ` · Template „${tmpl.name}“` : ""));
      if (sb.automatic && sb.automation && sb.automation !== "MANUAL_NONE") {
        body.appendChild(el("div", { class: "muted", style: "font-size:11px" },
          "Automatik: " + (AUTOMATION_LABELS[sb.automation] || sb.automation)));
      }
    } else {
      body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
        SERVICE_OPTIONAL_HINT));
    }
    const row = el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => assignServiceFor(node.id) }, sb ? "Dienst ändern" : "Dienst zuweisen"));
    if (sb && sb.automatic) row.appendChild(el("button", { class: "btn small", onClick: () => editAutomation(node, sb) }, "Automatik…"));
    if (sb) row.appendChild(el("button", { class: "btn small danger", onClick: () => removeService(node.id) }, "Entfernen"));
    body.appendChild(row);
  }

  // Frist (T1): maximale erwartete Dauer des Schritts.
  const tc = (schema.time_constraints || {})[node.id];
  const hasFrist = tc && tc.max_duration_seconds != null;
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Frist (max. Dauer)"));
  body.appendChild(el("div", { class: hasFrist ? "" : "muted", style: "font-size:12px" },
    hasFrist ? formatDuration(tc.max_duration_seconds) : "keine – fließt bei gesetztem Prozess-Termin in die Terminprüfung (T2) ein."));
  if (tc && tc.target_lead_seconds != null) {
    body.appendChild(el("div", { style: "font-size:12px;margin-top:4px" },
      "Soll-Reaktionszeit ab Aktivierung: " + formatDuration(tc.target_lead_seconds)));
  }
  body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
    // Die ganze Constraint weitergeben, nicht nur die Dauer: der Dialog liest
    // daraus BEIDE Soll-Zeiten vor (Dauer und Soll-Reaktionszeit).
    el("button", { class: "btn small", onClick: () => setTimeConstraintFor(node.id, hasFrist ? tc : null) }, hasFrist ? "Frist ändern" : "Frist setzen"),
    hasFrist ? el("button", { class: "btn small danger", onClick: () => removeTimeConstraint(node.id) }, "Entfernen") : null));
  escalationBlock(body, schema, node, true);

  // Priorität (E8): Auswirkung + Dringlichkeit -> abgeleitete Arbeitslisten-Stufe.
  const pr = (schema.node_priorities || {})[node.id];
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Priorität"));
  body.appendChild(el("div", { class: pr ? "" : "muted", style: "font-size:12px" },
    pr ? `Auswirkung ${IMPACT_LABELS[pr.impact] || pr.impact} · Dringlichkeit ${IMPACT_LABELS[pr.urgency] || pr.urgency}` : "normal (Standard)"));
  body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
    el("button", { class: "btn small", onClick: () => setPriorityFor(node.id, pr) }, pr ? "Priorität ändern" : "Priorität setzen"),
    pr ? el("button", { class: "btn small danger", onClick: () => removePriority(node.id) }, "Entfernen") : null));

  // Wertklasse (E3): rein beratende Klassifikation (keine Korrektheitswirkung).
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Wertklasse"));
  const vcSel = el("select", null,
    el("option", { value: "" }, "– keine –"),
    ...Object.entries(VALUE_CLASS_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  vcSel.value = node.value_class || "";
  vcSel.addEventListener("change", () => setValueClass(node.id, vcSel.value || null));
  body.appendChild(el("label", { class: "field", style: "font-size:12px" }, "Klassifikation", vcSel));

  // E-Mail-Benachrichtigung (Regelgruppe N): opt-in je Aktivität.
  nodeMailSection(body, schema, node);
}

const MAIL_MODE_LABELS = {
  TO_ELIGIBLE_AGENTS: "an die berechtigten Bearbeiter",
  TO_GROUP_MAILBOX: "an das Gruppenpostfach (Rolle/Abteilung)",
};

// Abschnitt „E-Mail bei Aufgabeneingang“ im Aktivitäts-Inspektor. Trägt keine
// Korrektheitslogik: der Kern verlangt eine BZR (N2) und vollständige, wohlgeformte
// Adressen (N3) und lehnt sonst das Speichern mit 422 ab – der konkrete Befund
// (welcher Bearbeiter/welche Gruppe keine Adresse hat) erscheint dann im Toast.
function nodeMailSection(body, schema, node) {
  if (node.type !== NODE_TYPE.ACTIVITY) return;
  const binding = (schema.mail_bindings || {})[node.id];
  const hasRule = !!(schema.staff_rules || {})[node.id];
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "E-Mail bei Aufgabeneingang"));
  if (binding) {
    body.appendChild(el("div", { style: "font-size:12px" },
      MAIL_MODE_LABELS[binding.mode] || binding.mode,
      binding.mode === "TO_ELIGIBLE_AGENTS"
        ? (binding.include_deputies ? " · inkl. Vertreter" : " · ohne Vertreter") : ""));
    body.appendChild(el("div", { class: "muted", style: "font-size:11px;margin-top:2px" },
      "Betreff: " + (binding.subject || "–")));
  } else if (!hasRule) {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Erst einen Bearbeiter zuordnen – ohne Zuordnung gibt es keinen Empfänger (N2)."));
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Aus – bei Aufgabeneingang wird keine Mail gesendet (Standard)."));
  }
  const row = el("div", { class: "row", style: "gap:8px;margin-top:6px" },
    el("button", { class: "btn small", disabled: !hasRule && !binding,
      onClick: () => editMailBinding(node.id, binding) }, binding ? "Mail ändern" : "Mail senden…"));
  if (binding) row.appendChild(el("button", { class: "btn small danger",
    onClick: () => removeMailBinding(node.id) }, "Entfernen"));
  body.appendChild(row);
}

// Modal zum Anlegen/Ändern des Mailversands. Der Platzhalter-Picker bietet nur die
// an diesem Schritt gelesenen Datenelemente an: eine Lesebindung ist durch D2
// „kein Read ohne vorheriges Set“ bei Aufgabeneingang garantiert gesetzt, sodass
// N4 (Vorlagen-Integrität) gar nicht erst verletzt werden kann.
function editMailBinding(nodeId, current) {
  const schema = state.schema;
  const modeSel = el("select", null,
    ...Object.entries(MAIL_MODE_LABELS).map(([v, l]) => el("option", { value: v }, l)));
  modeSel.value = current ? current.mode : "TO_ELIGIBLE_AGENTS";
  const depBox = el("input", { type: "checkbox" });
  depBox.checked = current ? !!current.include_deputies : true;
  const depWrap = el("label", { class: "row", style: "gap:8px;align-items:center" },
    depBox, "Vertreter einbeziehen");
  const subject = el("input", { type: "text", value: current ? current.subject : "",
    placeholder: "z. B. Neue Aufgabe: {kundenname}" });
  const bodyArea = el("textarea", { rows: "4",
    placeholder: "Optionaler Text mit {platzhalter}" }, current ? current.body : "");
  const readable = (schema.data_accesses || [])
    .filter((a) => a.node_id === nodeId && a.mode === "READ")
    .map((a) => schema.data_elements[a.element_id]).filter(Boolean);
  let lastField = subject;
  subject.addEventListener("focus", () => { lastField = subject; });
  bodyArea.addEventListener("focus", () => { lastField = bodyArea; });
  const insert = (id) => {
    const f = lastField;
    const start = f.selectionStart != null ? f.selectionStart : f.value.length;
    const end = f.selectionEnd != null ? f.selectionEnd : start;
    f.value = f.value.slice(0, start) + "{" + id + "}" + f.value.slice(end);
    f.focus();
  };
  const picker = readable.length
    ? el("div", { class: "row", style: "gap:6px;flex-wrap:wrap" },
        ...readable.map((e) => el("button", { class: "btn small", type: "button",
          onClick: () => insert(e.id) }, "{" + e.id + "}")))
    : el("div", { class: "muted", style: "font-size:11px" },
        "Keine an diesem Schritt garantiert gesetzten Datenelemente – Platzhalter erst nach einer Lesebindung.");
  const syncDep = () => {
    depWrap.style.display = modeSel.value === "TO_ELIGIBLE_AGENTS" ? "" : "none";
  };
  modeSel.addEventListener("change", syncDep); syncDep();
  openModal(`E-Mail – ${nodeCaption(schema.nodes[nodeId])}`,
    el("div", { style: "display:flex;flex-direction:column;gap:10px" },
      el("div", { class: "muted", style: "font-size:12px" },
        "Empfänger ergeben sich aus der Bearbeiterzuordnung dieses Schritts. Gesendet wird nur, wenn jede mögliche Adresse vorhanden ist (N3)."),
      el("label", { class: "field" }, "Empfänger", modeSel),
      depWrap,
      el("label", { class: "field" }, "Betreff", subject),
      el("label", { class: "field" }, "Text (optional)", bodyArea),
      el("div", { class: "field" },
        el("span", { class: "muted", style: "font-size:11px" }, "Platzhalter einfügen:"), picker)),
    async () => {
      const binding = {
        mode: modeSel.value, include_deputies: depBox.checked,
        subject: subject.value, body: bodyArea.value,
      };
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/mail-binding`, { node_id: nodeId, binding }), "Mailversand gesetzt");
    }, "Speichern");
}

// Entfernt den Mailversand an einem Schritt (POST … mail-binding mit binding:null).
async function removeMailBinding(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/mail-binding`, { node_id: nodeId, binding: null }), "Mailversand entfernt");
}

// Dienst zuweisen/aendern. Optional an ein Activity-Repository-Template gebunden
// (dann wird ``automatic`` aus dem Executor abgeleitet und die Parameter werden
// typkonform auf Datenelemente abgebildet -> A1-A3). Ohne Template ein freier,
// benannter Dienst. Der Kern prueft die Bindung (422 bei Verstoss).
function assignServiceFor(nodeId) {
  const schema = state.schema;
  const sb = (schema.service_bindings || {})[nodeId];
  const templates = Object.values(schema.activity_templates || {});
  const nameInput = el("input", { type: "text", value: sb ? sb.name : "", placeholder: "z. B. Antrag erfassen" });
  const autoBox = el("input", { type: "checkbox" });
  autoBox.checked = sb ? !!sb.automatic : false;
  const autoField = el("label", { class: "row", style: "gap:8px;align-items:center" },
    autoBox, "Automatischer Schritt (kein Bearbeiter nötig)");
  const tmplSel = templates.length
    ? el("select", null, el("option", { value: "" }, "– ohne Template (freier Dienst) –"),
        ...templates.map((t) => el("option", { value: t.id }, `${t.name} (${t.executor})`)))
    : null;
  if (tmplSel && sb && sb.template_id) tmplSel.value = sb.template_id;
  const mapHost = el("div");
  const buildMap = () => {
    clear(mapHost); mapHost._read = null;
    if (!tmplSel || !tmplSel.value) { autoBox.disabled = false; return; }
    const t = templates.find((x) => x.id === tmplSel.value);
    if (!t) return;
    // Bei Template-Bindung wird ``automatic`` aus dem Executor abgeleitet (A2).
    autoBox.checked = t.executor !== "MANUAL"; autoBox.disabled = true;
    const form = templateMappingForm(t, schema, sb && sb.template_id === t.id ? sb.parameter_mapping : {});
    mapHost._read = form.read;
    mapHost.appendChild(el("div", { class: "muted", style: "font-size:12px;margin:6px 0" },
      "Parameter-Zuordnung: jeden Template-Parameter typkonform auf ein Datenelement abbilden (A3)."));
    mapHost.appendChild(form.grid);
  };
  if (tmplSel) tmplSel.addEventListener("change", buildMap);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name des Dienstes", nameInput),
    tmplSel ? el("label", { class: "field" }, "Template (Activity Repository)", tmplSel) : null,
    autoField, mapHost);
  buildMap();
  openModal(`Dienst – ${nodeCaption(schema.nodes[nodeId])}`, body, async () => {
    if (!nameInput.value.trim()) { toast("err", "Name darf nicht leer sein"); return false; }
    const req = { node_id: nodeId, name: nameInput.value.trim(), automatic: autoBox.checked };
    if (tmplSel && tmplSel.value) {
      req.template_id = tmplSel.value;
      req.parameter_mapping = mapHost._read ? mapHost._read() : {};
    }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/service`, req), "Dienst zugewiesen", [req.name]);
  }, "Übernehmen");
}

// Zuordnungsformular fuer die Template-Parameter (Eingaben + Ergebnisse). Bietet
// je Parameter nur typgleiche Datenelemente an; der Kern prueft A3 verbindlich.
function templateMappingForm(template, schema, current) {
  const params = [
    ...(template.inputs || []).map((p) => ({ ...p, dir: "Eingabe" })),
    ...(template.outputs || []).map((p) => ({ ...p, dir: "Ergebnis" })),
  ];
  const elems = Object.values(schema.data_elements || {});
  const grid = el("div", { class: "form-grid" });
  const rows = [];
  if (!params.length) grid.appendChild(el("div", { class: "muted", style: "font-size:12px" }, "Dieses Template hat keine Parameter."));
  params.forEach((p) => {
    const sel = el("select", null,
      el("option", { value: "" }, p.mandatory ? "– bitte wählen –" : "– keine –"),
      ...elems.filter((e) => e.data_type === p.data_type).map((e) => el("option", { value: e.id }, e.name)));
    if (current && current[p.name]) sel.value = current[p.name];
    rows.push({ name: p.name, sel });
    grid.appendChild(el("label", { class: "field" }, `${p.dir}: ${p.name} (${typeName(p.data_type)})`, sel));
  });
  const read = () => { const m = {}; rows.forEach((r) => { if (r.sel.value) m[r.name] = r.sel.value; }); return m; };
  return { grid, read };
}

async function removeService(nodeId) {
  await commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/service/${nodeId}`), "Dienst entfernt");
}

function setTimeConstraintFor(nodeId, current) {
  // ``current`` ist die getragene Constraint (oder null). Zwei getrennte
  // Soll-Zeiten: die Bearbeitungs-
  // dauer ab Start (T2/kritischer Pfad) und die optionale Reaktionszeit ab
  // Aktivierung, die die zeitbasierte Arbeitslisten-Priorisierung steuert.
  const tc = current || {};
  const dur = durationControls(tc.max_duration_seconds != null ? tc.max_duration_seconds : null);
  const lead = durationControls(tc.target_lead_seconds != null ? tc.target_lead_seconds : null);
  // Netto-Zeit (E2 Stufe C, Opt-in): hält eine Pause die Fällig-Uhr an?
  // Default aus – ohne bewusste Entscheidung des Modellierers bleibt Anhalten
  // reine Transparenz und verschiebt weder Frist noch Eskalation.
  const pauseBox = el("input", { type: "checkbox" });
  pauseBox.checked = !!tc.pause_stops_clock;
  openModal(`Frist – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Maximale erwartete Bearbeitungsdauer dieses Schritts (fließt in die Terminprüfung T2 ein)."),
      dur.node,
      el("div", { class: "muted", style: "font-size:12px;margin:12px 0 8px" },
        "Optionale Soll-Reaktionszeit ab Aktivierung: bis wann die Aufgabe angefasst/erledigt sein soll. " +
        "Steuert die automatische Reihung der Arbeitsliste (überfällige oben). Leer = die Bearbeitungsdauer gilt."),
      lead.node,
      el("label", { class: "row", style: "gap:8px;align-items:center;font-size:12px;margin-top:12px;cursor:pointer" },
        pauseBox,
        el("span", null, "Pause hält die Uhr an (Netto-Zeit): angehaltene Zeit zählt nicht auf Frist und Eskalation. " +
          "Ohne Häkchen läuft die Uhr in Pausen bewusst weiter."))),
    async () => {
      const sec = dur.read();
      if (sec == null) { toast("err", "Bitte eine Dauer > 0 angeben"); return false; }
      const leadSec = lead.read();  // null = nicht gesetzt (Fallback-Regel S)
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/time-constraint`, {
        node_id: nodeId,
        constraint: { max_duration_seconds: sec, target_lead_seconds: leadSec,
          pause_stops_clock: pauseBox.checked },
      }), "Frist gesetzt");
    }, "Speichern");
}

async function removeTimeConstraint(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/time-constraint`, { node_id: nodeId, constraint: null }), "Frist entfernt");
}

// --- Synchronisation (K4) --------------------------------------------------
// EINE geteilte Blockfunktion für beide Modellier-Oberflächen: zeigt die
// Sync-Kanten eines Schritts (wartet auf / kommt vor) und bietet Setzen/
// Lösen an. Zulässigkeit (verschiedene Zweige EINES AND-Blocks, keine
// Zyklen) prüft der Kern (K4, Validate-before-Commit) – der Client zeigt
// Befunde nur an.
function syncBlock(body, schema, node, draft) {
  if (node.type !== NODE_TYPE.ACTIVITY) return;
  const touching = syncEdges(schema).filter((e) => e.source === node.id || e.target === node.id);
  if (!touching.length && !draft) return;
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Synchronisation (parallel)"));
  if (touching.length) {
    touching.forEach((e) => {
      const otherId = e.source === node.id ? e.target : e.source;
      const other = schema.nodes[otherId];
      const caption = other ? nodeCaption(other) : otherId;
      const label = e.source === node.id
        ? `läuft vor „${caption}“ (der wartet)`
        : `wartet auf „${caption}“`;
      body.appendChild(el("div", { class: "row", style: "gap:6px;align-items:center;font-size:12px" },
        el("span", null, label),
        draft ? el("button", { class: "btn small danger",
          onClick: () => removeSyncEdgeFor(e.source, e.target) }, "Lösen") : null));
    });
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "keine – eine Sync-Kante ordnet diesen Schritt zeitlich gegen einen Schritt eines anderen Parallelzweigs (warten auf Abschluss oder Abwahl)."));
  }
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => addSyncEdgeFor(node.id) }, "Sync-Kante hinzufügen…")));
  }
}

function addSyncEdgeFor(nodeId) {
  const acts = Object.values(state.schema.nodes)
    .filter((n) => n.type === NODE_TYPE.ACTIVITY && n.id !== nodeId)
    .sort((a, b) => (a.label || "").localeCompare(b.label || ""));
  if (!acts.length) { toast("err", "Keine weitere Aktivität vorhanden"); return; }
  const target = el("select", null, ...acts.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const dir = el("select", null,
    el("option", { value: "waits" }, "dieser Schritt wartet auf den gewählten"),
    el("option", { value: "before" }, "der gewählte Schritt wartet auf diesen"));
  openModal(`Sync-Kante – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Ordnet zwei Schritte verschiedener Parallelzweige zeitlich: Der wartende Schritt beginnt erst, wenn der andere abgeschlossen (oder abgewählt) ist. Zulässig nur zwischen verschiedenen Zweigen desselben UND-Blocks (K4); der Kern prüft vor dem Speichern."),
      el("label", { class: "field" }, "Anderer Schritt", target),
      el("label", { class: "field" }, "Richtung", dir)),
    async () => {
      const src = dir.value === "waits" ? target.value : nodeId;
      const dst = dir.value === "waits" ? nodeId : target.value;
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/sync-edge`, { source_id: src, target_id: dst }), "Sync-Kante gesetzt");
    }, "Hinzufügen");
}

async function removeSyncEdgeFor(sourceId, targetId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/sync-edge/remove`, { source_id: sourceId, target_id: targetId }), "Sync-Kante gelöst");
}

// Querschritt zwischen Knotenmengen (ADEPT insertBetweenNodeSets): neuer
// Parallelzweig, per Sync-Kanten nach den Quellen und vor den Zielen.
function insertBetweenDialog() {
  const acts = Object.values(state.schema.nodes)
    .filter((n) => n.type === NODE_TYPE.ACTIVITY)
    .sort((a, b) => (a.label || "").localeCompare(b.label || ""));
  if (acts.length < 2) { toast("err", "Dafür braucht es mindestens zwei Aktivitäten"); return; }
  const label = el("input", { type: "text", placeholder: "z. B. Zwischenprüfung" });
  const mk = () => el("select", { multiple: true, size: "5" },
    ...acts.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const src = mk(), dst = mk();
  openModal("Querschritt einfügen (zwischen Knotenmengen)",
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Der neue Schritt läuft als eigener Parallelzweig dieses UND-Blocks: erst nachdem alle Quell-Schritte erledigt (oder abgewählt) sind, und vor allen Ziel-Schritten (Sync-Kanten, K4). Quellen und Ziele müssen im selben UND-Block liegen; der Kern prüft vor dem Speichern."),
      el("label", { class: "field" }, "Bezeichnung", label),
      el("label", { class: "field" }, "Nach diesen Schritten (Quellen, Mehrfachauswahl)", src),
      el("label", { class: "field" }, "Vor diesen Schritten (Ziele, Mehrfachauswahl)", dst)),
    async () => {
      const sources = [...src.selectedOptions].map((o) => o.value);
      const targets = [...dst.selectedOptions].map((o) => o.value);
      if (!label.value.trim() || !sources.length || !targets.length) {
        toast("err", "Bezeichnung, Quellen und Ziele angeben"); return false;
      }
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/insert-between`,
        { label: label.value.trim(), source_ids: sources, target_ids: targets }), "Querschritt eingefügt");
    }, "Einfügen");
}

// --- Eskalation (T3/E9) ----------------------------------------------------
// Anzeige + Bearbeitung der modellierten Fristverletzungs-Reaktion. EINE
// geteilte Blockfunktion für beide Modellier-Oberflächen (Schritt-Karte und
// klassischer Inspektor), damit die Sichten nie driften. Die Wohlgeformtheit
// (Soll-Zeit vorhanden, Stufen aufsteigend, Ziele auflösbar) prüft der Kern
// (T3a–T3c, Validate-before-Commit) – der Client zeigt Befunde nur an.
function escalationBlock(body, schema, node, draft) {
  if (node.type !== NODE_TYPE.ACTIVITY) return;
  const policy = (schema.escalation_policies || {})[node.id];
  const roles = (schema.org_model || {}).roles || {};
  const roleName = (ref) => (roles[ref] ? roles[ref].name : ref);
  body.appendChild(el("div", { class: "hr" }));
  body.appendChild(el("div", { class: "insp-h" }, "Eskalation bei Fristüberschreitung"));
  if (policy && (policy.stages || []).length) {
    policy.stages.forEach((s, i) => {
      const when = s.after_seconds > 0
        ? formatDuration(s.after_seconds) + " nach Fristüberschreitung"
        : "bei Fristüberschreitung";
      const what = s.kind === "FUNCTIONAL"
        ? `zusätzlich anbieten an „${roleName(s.rule && s.rule.ref)}“`
        : `informieren: „${roleName(s.rule && s.rule.ref)}“`;
      body.appendChild(el("div", { style: "font-size:12px" }, `Stufe ${i + 1}: ${when} – ${what}`));
    });
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "keine – eine Überschreitung bleibt nur im Kritikalitätsband der Arbeitsliste sichtbar."));
  }
  if (draft) {
    body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
      el("button", { class: "btn small", onClick: () => setEscalationFor(node.id, policy || null) },
        policy ? "Eskalation ändern" : "Eskalation einrichten"),
      policy ? el("button", { class: "btn small danger", onClick: () => removeEscalation(node.id) }, "Entfernen") : null));
  }
}

function setEscalationFor(nodeId, current) {
  const roles = Object.values((state.schema.org_model || {}).roles || {});
  if (!roles.length) { toast("err", "Erst Rollen in der Organisation anlegen"); return; }
  // Schlichte Spalte statt ``.row``: Dessen Umbruch (flex-wrap) mass das
  // Stufenraster bei Minimalbreite und liess darueber eine grosse Luecke.
  const rows = el("div", { style: "display:flex;flex-direction:column;gap:8px" });
  function addRow(stage) {
    const after = el("input", { type: "number", class: "esc-after", min: "0",
      value: stage ? String(Math.round(stage.after_seconds / 60)) : "0" });
    const kind = el("select", { class: "esc-kind" },
      el("option", { value: "FUNCTIONAL" }, "zusätzlich anbieten (funktional)"),
      el("option", { value: "HIERARCHICAL" }, "informieren (hierarchisch)"));
    if (stage) kind.value = stage.kind;
    const role = el("select", { class: "esc-role" },
      ...roles.map((r) => el("option", { value: r.id }, r.name)));
    if (stage && stage.rule && stage.rule.ref) role.value = stage.rule.ref;
    rows.appendChild(el("div", { class: "branch-row esc-row" },
      el("label", { class: "field" }, "Minuten nach Frist", after),
      el("label", { class: "field" }, "Art", kind),
      el("label", { class: "field" }, "Ziel-Rolle", role)));
  }
  ((current && current.stages && current.stages.length) ? current.stages : [null]).forEach(addRow);
  openModal(`Eskalation – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Überschreitet die Aufgabe ihre Soll-Zeit, feuern die Stufen nacheinander: „zusätzlich anbieten“ erweitert den Bearbeiterkreis (funktionale Eskalation), „informieren“ benachrichtigt die Ziel-Rolle, ohne sie zu berechtigen (hierarchisch). Voraussetzung ist eine Frist bzw. Soll-Reaktionszeit am Schritt."),
      rows,
      el("button", { class: "btn small ghost", onClick: () => addRow(null) }, "+ Stufe")),
    async () => {
      const stages = [...rows.querySelectorAll(".esc-row")].map((r) => ({
        after_seconds: Number(r.querySelector(".esc-after").value || "0") * 60,
        kind: r.querySelector(".esc-kind").value,
        rule: { kind: "ROLE", ref: r.querySelector(".esc-role").value },
      }));
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/escalation-policy`, { node_id: nodeId, policy: { stages } }), "Eskalation gespeichert");
    }, "Speichern");
}

async function removeEscalation(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/escalation-policy`, { node_id: nodeId, policy: null }), "Eskalation entfernt");
}

function setPriorityFor(nodeId, current) {
  const mk = (val) => {
    const s = el("select", null, ...Object.entries(IMPACT_LABELS).map(([v, l]) => el("option", { value: v }, l)));
    s.value = val || "MEDIUM"; return s;
  };
  const impact = mk(current && current.impact), urgency = mk(current && current.urgency);
  openModal(`Priorität – ${nodeCaption(state.schema.nodes[nodeId])}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Priorität = Auswirkung + Dringlichkeit; die Arbeitslisten-Reihung ergibt sich daraus."),
      el("div", { class: "form-grid" },
        el("label", { class: "field" }, "Auswirkung", impact),
        el("label", { class: "field" }, "Dringlichkeit", urgency))),
    async () => {
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/priority`, { node_id: nodeId, priority: { impact: impact.value, urgency: urgency.value } }), "Priorität gesetzt");
    }, "Speichern");
}

async function removePriority(nodeId) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/priority`, { node_id: nodeId, priority: null }), "Priorität entfernt");
}

async function setValueClass(nodeId, value) {
  await commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/value-class`, { node_id: nodeId, value_class: value }), value ? "Wertklasse gesetzt" : "Wertklasse entfernt");
}

// Prozessweiter Termin (T2): der Kern prueft, dass der kritische Pfad
// hineinpasst. Leer lassen entfernt den Termin.
function setProcessDeadline() {
  const dur = durationControls(state.schema.deadline_seconds != null ? state.schema.deadline_seconds : null);
  openModal("Prozess-Termin (Deadline)",
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Harte Frist für den gesamten Prozess; der Kern prüft, dass der kritische Pfad hineinpasst (T2). Leer lassen entfernt den Termin."),
      dur.node),
    async () => {
      const sec = dur.read();
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/deadline`, { deadline_seconds: sec }), sec != null ? "Termin gesetzt" : "Termin entfernt");
    }, "Speichern");
}

function centerCanvasOnNode(wrap, pos, region) {
  // ``wrap`` ist die .canvas-wrap; der Pan/Zoom-Controller verschiebt den
  // Knoten ueber eine CSS-Transformation in die Mitte des Viewports (statt
  // ueber nativen Scroll, der durch overflow:hidden entfaellt). ``region``
  // (optional) haelt zusaetzlich die gestrichelte Datenherkunft im Bild.
  if (wrap && wrap._panzoom) wrap._panzoom.centerOn(pos, region);
}

// --------------------------------------------------------------------------
// Knoten-Inspektor (Aktivitaet umbenennen / Element entfernen)
// --------------------------------------------------------------------------

const SPLIT_TYPES = new Set([NODE_TYPE.AND_SPLIT, NODE_TYPE.XOR_SPLIT]);

// Liefert die Join-Knoten-ID, wenn der XOR-Split ``splitId`` einen leeren Zweig
// traegt (ein Zweig, der direkt auf den Join zeigt, weil seine Aktivitaet
// geloescht wurde), sonst null. Ein leerer Zweig behaelt seine K7-Zelle, sodass
// nur im anderen Zweig Arbeit anfaellt.
function emptyBranchJoin(schema, splitId) {
  const node = schema.nodes[splitId];
  if (!node || node.type !== NODE_TYPE.XOR_SPLIT) return null;
  for (const e of controlEdges(schema)) {
    if (e.source !== splitId) continue;
    const t = schema.nodes[e.target];
    if (t && t.type === NODE_TYPE.XOR_JOIN) return e.target;
  }
  return null;
}

/**
 * Knopf „Neue Revision" fuer ein freigegebenes Schema (ueberall gleich benannt).
 *
 * Steht dort, wo der Nutzer auf die Wand laeuft (Knoten-Inspektor), nicht nur
 * im Panel „Schema-Evolution" ganz unten in der rechten Spalte: Der Hinweis
 * „Bearbeiten erst in einer neuen Revision moeglich" nannte bisher die Loesung,
 * ohne sie anzubieten -- man musste sie woanders suchen.
 *
 * Loest keine eigene Logik aus, sondern ruft dieselbe Operation wie das Panel
 * (``newRevision`` -> ``POST /schemas/{id}/revision``). Ohne Modellierer-/
 * Admin-Rolle bleibt nur der erklaerende Text, weil der Aufruf sonst ohnehin
 * abgelehnt wuerde.
 *
 * @returns {HTMLElement} Der Knopf, oder ein leeres Element ohne Berechtigung.
 */
function newRevisionAction() {
  if (!hasRole("modeler", "admin")) return el("div");
  return el("div", { class: "row", style: "gap:8px;margin-top:10px" },
    el("button", {
      class: "btn small primary", onClick: newRevision,
      title: "Bearbeitbare Entwurfskopie dieser Revision anlegen",
    }, "Neue Revision"));
}

function nodeInspectorPanel() {
  const schema = state.schema;
  const draft = isDraft(schema);
  const body = el("div", { class: "panel-b" });
  const node = state.selectedNode ? schema.nodes[state.selectedNode] : null;

  if (!node) {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      draft
        ? "Klicke einen Knoten an, um ihn umzubenennen oder zu entfernen."
        : "Klicke einen Knoten an, um zu ihm zu scrollen. Bearbeiten ist nur im Entwurf m\u00F6glich."));
    if (!draft) body.appendChild(newRevisionAction());
    return el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, "Knoten")), body);
  }

  body.appendChild(el("div", { class: "row", style: "gap:8px;align-items:center;margin-bottom:10px" },
    el("span", { class: "pill pill-gray", title: node.type }, nodeTypeLabel(node.type)),
    el("strong", null, nodeCaption(node))));

  const renamable = node.type === NODE_TYPE.ACTIVITY || node.type === NODE_TYPE.SUBPROCESS;
  if (!draft) {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Schema ist freigegeben \u2013 zum Bearbeiten eine neue Revision anlegen (Knoten-IDs bleiben erhalten)."));
    body.appendChild(newRevisionAction());
  } else if (renamable) {
    const input = el("input", { type: "text", id: "insp-name-input", value: node.label || "" });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") renameNode(node.id, input.value); });
    body.appendChild(el("label", { class: "field" }, "Bezeichnung", input));
    body.appendChild(el("div", { class: "row", style: "gap:8px" },
      el("button", { class: "btn small primary", onClick: () => renameNode(node.id, input.value) }, "Umbenennen"),
      el("button", { class: "btn small", title: "Schritt an eine andere Stelle des Ablaufs verschieben – alle Bindungen bleiben erhalten",
        onClick: () => moveNodeDialog(node.id) }, "Verschieben…"),
      el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Entfernen")));
    if (node.type === NODE_TYPE.ACTIVITY) {
      // --- Datenbindungen dieses Schritts (D1-D4) direkt am Schritt ---
      const accesses = (schema.data_accesses || []).filter((a) => a.node_id === node.id);
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "insp-h" }, "Daten"));
      if (accesses.length) {
        const list = el("div", { class: "insp-binds" });
        accesses.forEach((a) => {
          const e = schema.data_elements[a.element_id];
          list.appendChild(el("div", { class: "insp-bind" },
            el("span", { class: "insp-bind-name" }, e ? e.name : a.element_id),
            el("span", { class: "insp-bind-mode mode-" + a.mode, title: a.mode }, accessModeLabel(a.mode)),
            a.mandatory ? null : el("span", { class: "muted", style: "font-size:11px" }, "optional"),
            el("span", { class: "spacer", style: "flex:1" }),
            el("button", { class: "insp-bind-del", title: "Bindung lösen",
              onClick: () => removeDataAccess(node.id, a.element_id, a.mode, e ? e.name : a.element_id) }, "✕")));
        });
        body.appendChild(list);
      } else {
        body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
          "Noch keine Datenbindung – rechts unter „Binden“ ein Datenelement mit ⊕ an diesen Schritt zuweisen."));
      }
      // Zuweisen läuft bewusst über die Binden-Palette (⊕); hier nur die
      // aktuellen Bindungen dieses Schritts anzeigen und einzeln lösen (✕).

      // --- Bearbeiterzuordnung (Z1-Z4) direkt am Schritt ---
      const rule = (schema.staff_rules || {})[node.id];
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "insp-h" }, "Bearbeiter (BZR)"));
      body.appendChild(el("div", { class: rule ? "" : "muted", style: "font-size:12px" },
        rule ? describeRule(rule) : "Keine Zuordnung – hier zuordnen oder rechts unter „Binden“ eine Rolle/Abteilung mit ⊕ an diesen Schritt ziehen."));
      // Derselbe Dialog wie in der Schritt-Karte (bindStaffDialog: Auswahl,
      // „Erweiterte Regel“, „Auch weiteren Schritten zuordnen“) -- beide
      // Oberflaechen bleiben gleichwertig.
      body.appendChild(el("div", { class: "row", style: "gap:8px;margin-top:6px" },
        el("button", { class: "btn small", onClick: () => bindStaffDialog(node.id) },
          rule ? "Ändern …" : "Zuordnen …"),
        rule ? el("button", { class: "btn small danger", onClick: () => removeStaffRule(node.id) }, "Entfernen") : null));

      // Weitere Standardaktivitäten am Schritt: Dienst, Frist, Priorität, Wertklasse.
      nodePerformSections(body, schema, node);

      const form = schema.forms && schema.forms[node.id];
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
        form
          ? `Eingabemaske: ${countLabel(form.fields.length, "Feld", "Felder")}${form.title ? " \u2013 \u201E" + form.title + "\u201C" : ""}.`
          : "Noch keine Eingabemaske \u2013 Felder per Auswahl zusammenstellen."));
      const row = el("div", { class: "row", style: "gap:8px" },
        el("button", { class: "btn small", onClick: () => openFormDesigner(node.id) },
          form ? "Maske bearbeiten" : "Eingabemaske gestalten"));
      if (form) {
        row.appendChild(el("button", { class: "btn small danger", onClick: () => deleteForm(node.id) }, "Maske entfernen"));
      }
      body.appendChild(row);
      // Wiederverwendung: Aktivitaet durch ein freigegebenes Submodell ersetzen.
      // Die Bindung gilt Correct-by-Construction: der Kern lehnt sie ab, wenn
      // das Gesamtmodell dadurch inkonsistent oder nicht lauffaehig wuerde.
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
        "Wiederverwendung: diesen Schritt durch ein freigegebenes Submodell aus der Bibliothek ersetzen \u2013 inkl. Daten\u00FCbergabe."));
      body.appendChild(el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "convert") }, "In Subprozess umwandeln"));
    } else if (node.type === NODE_TYPE.SUBPROCESS) {
      const bnd = (schema.sub_process_bindings || {})[node.id];
      body.appendChild(el("div", { class: "hr" }));
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
        bnd
          ? `Gebunden an Submodell \u201E${bnd.target_schema_id}\u201C (v${bnd.target_version}).`
          : "Noch kein Submodell gebunden."));
      body.appendChild(el("button", { class: "btn small", onClick: () => openSubprocessBinding(node, "rebind") },
        "Zuordnung / Daten\u00FCbergabe \u00E4ndern"));
      // Frist, Priorit\u00E4t und Wertklasse gelten auch f\u00FCr Subprozesse.
      nodePerformSections(body, schema, node);
    }
  } else if (SPLIT_TYPES.has(node.type)) {
    // Leerer Zweig (nur XOR): entsteht, wenn die letzte Aktivit\u00E4t eines Zweigs
    // entfernt wurde. Er bleibt als direkter Split\u2013Join-Zweig stehen, damit nur
    // im anderen Zweig Arbeit anf\u00E4llt; hier l\u00E4sst er sich gezielt aufl\u00F6sen.
    const emptyJoin = emptyBranchJoin(schema, node.id);
    if (emptyJoin) {
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Diese Verzweigung hat einen leeren Zweig \u2013 in ihm f\u00E4llt keine Aktivit\u00E4t an. Entfernen des leeren Zweigs l\u00F6st bei nur noch einem verbleibenden Zweig die ganze Verzweigung auf."));
      body.appendChild(el("button", { class: "btn small", onClick: () => removeEmptyBranch(node.id) }, "Leeren Zweig entfernen"));
      body.appendChild(el("div", { class: "hr" }));
    }
    if (node.type === NODE_TYPE.AND_SPLIT) {
      body.appendChild(el("button", { class: "btn small", style: "margin-bottom:8px",
        onClick: () => insertBetweenDialog() }, "Querschritt einf\u00FCgen\u2026"));
      body.appendChild(el("div", { class: "hr" }));
    }
    body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      "Verzweigung: Entfernen l\u00F6scht den gesamten Block (Split, Zweige und passenden Join)."));
    body.appendChild(el("button", { class: "btn small danger", onClick: () => deleteNode(node.id) }, "Verzweigung entfernen"));
  } else if (LOOP_TYPES.has(node.type)) {
    body.appendChild(loopNodePanel(schema, node, draft));
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      node.type === NODE_TYPE.AND_JOIN || node.type === NODE_TYPE.XOR_JOIN
        ? "Join-Knoten werden \u00FCber ihren \u00F6ffnenden Split entfernt."
        : "Start und Ende sind fester Bestandteil des Modells."));
  }
  return el("div", { class: "panel node-inspector" },
    el("div", { class: "panel-h" }, el("h2", null, "Knoten"),
      el("span", { class: "spacer", style: "flex:1" }),
      el("button", { class: "btn small ghost", onClick: () => { state.selectedNode = null; render(); } }, "Abw\u00E4hlen")),
    body);
}

async function renameNode(nodeId, label) {
  const name = (label || "").trim();
  if (!name) { toast("err", "Bezeichnung darf nicht leer sein"); return; }
  try {
    await api.patch(`/schemas/${state.schemaId}/nodes/${nodeId}`, { label: name });
    await refreshSchema();
    render();
    toast("ok", "Aktivit\u00E4t umbenannt", [name]);
  } catch (err) { toastError(err); }
}

function deleteNode(nodeId) {
  const node = state.schema.nodes[nodeId];
  const isSplit = SPLIT_TYPES.has(node.type);
  const msg = isSplit
    ? "Den gesamten Verzweigungsblock (Split, alle Zweige und den passenden Join) entfernen?"
    : node.type === NODE_TYPE.LOOP_START
      ? "Die gesamte Schleife (Anfang, Rumpf und Ende) entfernen?"
      : `\u201E${nodeCaption(node)}\u201C aus dem Modell entfernen?`;
  openModal("Element entfernen", el("div", { class: "muted", style: "font-size:13px" }, msg), async () => {
    try {
      await api.del(`/schemas/${state.schemaId}/nodes/${nodeId}`);
      state.selectedNode = null;
      await refreshSchema();
      render();
      toast("ok", "Element entfernt");
    } catch (err) { toastError(err); return false; }
  }, "Entfernen", { danger: true });
}

// --- Schritt verschieben (moveNode) ---------------------------------------
// Löst den Schritt aus seiner Position und fügt ihn hinter dem gewählten Anker
// wieder ein. Anders als Löschen + Neuanlegen bleibt die Knoten-ID erhalten,
// alle Bindungen (Daten, Bearbeiter, Dienst, Maske, Zeit, Benachrichtigung)
// reisen mit. Der Kern validiert vor dem Commit (D1/Z3/T2/N4 …) und lehnt ein
// unzulässiges Ziel mit lokalisierten Befunden ab – die Zielliste hier ist nur
// eine Anzeige-Vorauswahl, keine Korrektheitsentscheidung. Geteilte Funktion
// beider Modellier-Oberflächen (Schritt-Karte und klassische Sicht) – niemals
// je Sicht ausformulieren, sonst driften die Oberflächen auseinander.

// Plausible Anker: alle Knoten mit genau einem Ausgang (ein Split kann nie
// Anker sein), außer dem Schritt selbst, dem Ende und dem aktuellen
// Vorgänger (das wäre ein No-op).
function moveTargetsFor(nodeId) {
  const schema = state.schema;
  const outCount = {};
  controlEdges(schema).forEach((e) => { outCount[e.source] = (outCount[e.source] || 0) + 1; });
  const pred = controlEdges(schema).find((e) => e.target === nodeId);
  return Object.values(schema.nodes).filter((n) =>
    n.id !== nodeId &&
    n.type !== NODE_TYPE.END &&
    outCount[n.id] === 1 &&
    !(pred && n.id === pred.source));
}

function moveNodeDialog(nodeId) {
  const node = state.schema.nodes[nodeId];
  const targets = moveTargetsFor(nodeId);
  if (!targets.length) { toast("err", "Keine gültige Zielposition vorhanden"); return; }
  const sel = el("select", null,
    ...targets.map((n) => el("option", { value: n.id }, "nach „" + nodeCaption(n) + "“")));
  openModal("Schritt verschieben",
    el("div", null,
      el("div", { class: "muted", style: "font-size:13px;margin-bottom:8px" },
        `„${nodeCaption(node)}“ an eine andere Stelle des Ablaufs verschieben. ` +
        "Alle Bindungen des Schritts bleiben erhalten; ein Ziel, das eine Regel verletzen würde " +
        "(z. B. Lesen vor Schreiben), wird mit Begründung abgelehnt."),
      el("label", { class: "field" }, "Neue Position", sel)),
    async () => {
      try {
        await api.post(`/schemas/${state.schemaId}/nodes/${nodeId}/move`, { after_node_id: sel.value });
        await refreshSchema();
        render();
        toast("ok", "Schritt verschoben", [nodeCaption(node)]);
      } catch (err) { toastError(err); return false; }
    }, "Verschieben");
}

// --- Schleifenblock (K6) ---------------------------------------------------
// Beschreibung + Aktionen der Schleifen-Begrenzer, geteilt von beiden
// Modellier-Oberflächen (Schritt-Karte und klassischer Knoten-Inspektor).
// LOOP_START bietet das Entfernen der ganzen Schleife an (der Kern löscht
// Anfang, Rumpf, Ende und Entscheidung als Einheit); LOOP_END zeigt die
// strukturierte Abbruchentscheidung an – geändert wird sie nicht hier,
// sondern über das Wiederholen-Merkmal (Datenelement) selbst.
function loopNodePanel(schema, node, draft) {
  const box = el("div", null);
  if (node.type === NODE_TYPE.LOOP_END) {
    const d = (schema.loop_decisions || {})[node.id];
    const condition = loopConditionCaption(schema, d, 24);
    box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      d ? `Schleifenende: wiederholt wird, solange ${condition} ist. Der Rumpf läuft mindestens einmal; die Entscheidung fällt in jeder Runde automatisch aus den Daten (K6).`
        : "Schleifenende ohne hinterlegte Entscheidung."));
    if (d && d.max_iterations) {
      box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        `Notbremse: höchstens ${d.max_iterations} Durchläufe – am Limit wird die Schleife automatisch verlassen, und die Zeitprüfung (T2) rechnet den Rumpf ${d.max_iterations}-fach.`));
    }
    box.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Entfernt wird die Schleife über ihren Schleifenanfang."));
    return box;
  }
  box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
    "Schleifenanfang: Der Block bis zum Schleifenende wird wiederholt, bis das Wiederholen-Merkmal die Schleife beendet. Entfernen löscht die gesamte Schleife (Anfang, Rumpf, Ende)."));
  if (draft) {
    box.appendChild(el("button", { class: "btn small danger",
      onClick: () => deleteNode(node.id) }, "Schleife entfernen"));
  }
  return box;
}

// Leeren Zweig eines XOR-Splits gezielt entfernen. Bleibt danach nur ein Zweig
// übrig, löst der Kern die ganze Verzweigung auf (Split/Join verschwinden, der
// verbleibende Zweig wird inline eingefügt). Der Kern validiert vor dem Commit;
// ein ungültiges Ergebnis (z. B. verlorener Auffang-Zweig) wird abgelehnt.
function removeEmptyBranch(splitId) {
  openModal("Leeren Zweig entfernen",
    el("div", { class: "muted", style: "font-size:13px" },
      "Den leeren Zweig dieser Verzweigung entfernen? Verbleibt nur noch ein Zweig, wird die gesamte Verzweigung aufgelöst."),
    async () => {
      try {
        await api.post(`/schemas/${state.schemaId}/nodes/${splitId}/remove-empty-branch`, {});
        state.selectedNode = null;
        await refreshSchema();
        render();
        toast("ok", "Leerer Zweig entfernt");
      } catch (err) { toastError(err); return false; }
    }, "Entfernen", { danger: true });
}

async function deleteForm(nodeId) {
  openModal("Eingabemaske entfernen",
    el("div", { class: "muted", style: "font-size:13px" },
      "Die Eingabemaske dieses Schritts entfernen? Die zugeh\u00F6rigen Datenzugriffe der Maske werden mit gel\u00F6scht."),
    async () => {
      try {
        await api.del(`/schemas/${state.schemaId}/nodes/${nodeId}/form`);
        await refreshSchema();
        render();
        toast("ok", "Eingabemaske entfernt");
      } catch (err) { toastError(err); return false; }
    }, "Entfernen", { danger: true });
}

// --------------------------------------------------------------------------
// Wiederverwendbare Subprozesse (Submodell-Bibliothek + Datenuebergabe)
// --------------------------------------------------------------------------

// Kopf-Button: markiert dieses Modell als wiederverwendbares Submodell. Der
// Katalog-Flag ist reine Metadatenangabe (beeinflusst die Validierung nie);
// bindbar wird ein Submodell erst nach Freigabe (siehe /subprocess-library).
function libraryToggleButton(schema) {
  if (!hasRole("modeler", "admin")) return null;
  const on = schema.is_library_subprocess === true;
  return el("button", {
    class: "btn small" + (on ? " primary" : ""),
    title: "Dieses Modell als wiederverwendbares Submodell f\u00FCr die Bibliothek markieren (nach Freigabe in anderen Modellen bindbar).",
    onClick: () => toggleLibraryFlag(!on),
  }, on ? "\u2605 Submodell" : "\u2606 Als Submodell");
}

async function toggleLibraryFlag(flag) {
  try {
    await api.post(`/schemas/${state.schemaId}/library-flag`, { is_library: flag });
    await refreshSchema();
    render();
    toast("ok", flag ? "Als Submodell markiert" : "Submodell-Markierung entfernt");
  } catch (err) { toastError(err); }
}

// Baut das Zuordnungsformular fuer die Datenuebergabe: je Datenelement des
// Ziel-Submodells eine optionale Eingabe- (parent -> child) und Ergebnis-
// Zuordnung (child -> parent). Nur typgleiche Elternelemente werden angeboten
// (H2); der Kern prueft Typkonformitaet und Erzeugungsgarantie verbindlich.
function subprocessMappingForm(target, parentSchema) {
  const parentEls = Object.values((parentSchema && parentSchema.data_elements) || {});
  const grid = el("div", { class: "form-grid" });
  const rows = [];
  if (!target.data_elements.length) {
    grid.appendChild(el("div", { class: "muted", style: "font-size:12px" },
      "Das Submodell hat keine Datenelemente \u2013 es wird nur der Kontrollfluss eingebunden."));
  }
  target.data_elements.forEach((te) => {
    const options = () => [el("option", { value: "" }, "\u2013 keine \u2013"),
      ...parentEls.filter((pe) => pe.data_type === te.data_type)
        .map((pe) => el("option", { value: pe.id }, pe.name))];
    const inSel = el("select", null, ...options());
    const outSel = el("select", null, ...options());
    rows.push({ te, inSel, outSel });
    grid.appendChild(el("div", { class: "field" },
      el("div", { style: "font-weight:600;font-size:13px" }, `${te.name} (${typeName(te.data_type)})`),
      el("div", { class: "row", style: "gap:8px" },
        el("label", { class: "field", style: "flex:1" }, "Eingabe von", inSel),
        el("label", { class: "field", style: "flex:1" }, "Ergebnis nach", outSel))));
  });
  const read = () => {
    const input_mapping = {}, output_mapping = {};
    rows.forEach(({ te, inSel, outSel }) => {
      if (inSel.value) input_mapping[te.id] = inSel.value;
      if (outSel.value) output_mapping[te.id] = outSel.value;
    });
    return { input_mapping, output_mapping };
  };
  return { grid, read };
}

// Aktivitaet in einen Subprozess umwandeln ("convert") bzw. die Bindung eines
// bestehenden SUBPROCESS-Knotens aendern ("rebind"). Beides ist Correct by
// Construction: die Verbindung wird nur gesetzt, wenn das resultierende
// Gesamtmodell konsistent und lauffaehig bleibt (der Kern antwortet sonst 422).
async function openSubprocessBinding(node, mode) {
  let library;
  try { library = await api.get("/subprocess-library"); }
  catch (err) { toastError(err); return; }
  if (!library.length) {
    toast("info", "Keine freigegebenen Submodelle in der Bibliothek. Markiere zuerst ein freigegebenes Schema als Submodell.");
    return;
  }
  const bnd = (state.schema.sub_process_bindings || {})[node.id];
  const targetSel = el("select", null,
    ...library.map((t) => el("option", { value: t.id }, `${t.name} (v${t.version})`)));
  if (mode === "rebind" && bnd && library.some((t) => t.id === bnd.target_schema_id)) {
    targetSel.value = bnd.target_schema_id;
  }
  const mapHost = el("div");
  const buildMap = () => {
    const t = library.find((x) => x.id === targetSel.value);
    clear(mapHost);
    if (!t) return;
    const form = subprocessMappingForm(t, state.schema);
    mapHost._read = form.read;
    mapHost.appendChild(el("div", { class: "muted", style: "font-size:12px;margin:6px 0" },
      "Daten\u00FCbergabe: ordne die Elemente des Submodells den Datenelementen dieses Modells zu."));
    mapHost.appendChild(form.grid);
  };
  targetSel.addEventListener("change", buildMap);
  buildMap();
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Submodell aus Bibliothek", targetSel), mapHost);
  const isConvert = mode === "convert";
  openModal(isConvert ? "In Subprozess umwandeln" : "Zuordnung / Daten\u00FCbergabe \u00E4ndern",
    body, async () => {
      const t = library.find((x) => x.id === targetSel.value);
      if (!t) { toast("info", "Bitte ein Submodell w\u00E4hlen."); return false; }
      const { input_mapping, output_mapping } = mapHost._read ? mapHost._read() : { input_mapping: {}, output_mapping: {} };
      const path = isConvert ? "convert-to-subprocess" : "subprocess-binding";
      try {
        await api.post(`/schemas/${state.schemaId}/${path}`, {
          node_id: node.id,
          target_schema_id: t.id,
          target_version: t.version,
          input_mapping,
          output_mapping,
        });
        await refreshSchema();
        render();
        toast("ok", isConvert ? "Aktivit\u00E4t in Subprozess umgewandelt" : "Zuordnung aktualisiert");
      } catch (err) { toastError(err); return false; }
    }, isConvert ? "Umwandeln" : "\u00DCbernehmen");
}

/**
 * Wandelt eine Texteingabe in den Wert des Datentyps -- aber nur, wenn das
 * **eindeutig** geht. Alles andere bleibt der rohe Text, und der Kern lehnt
 * ihn mit einer D3-Meldung ab, die der Nutzer sieht.
 *
 * Frueher stand an zwei Stellen ``val === "true" || val === "1"``: „vielleicht"
 * im Ja/Nein-Feld wurde still zu ``false`` gespeichert, und der Vorgang lief
 * weiter. Ebenso machte ``parseInt`` aus „12,5"
 * still 12 und aus „abc" ``NaN``. Hier wird deshalb nie geraten.
 *
 * @param {string} dtype Datentyp des Elements (INTEGER, FLOAT, BOOLEAN, ...)
 * @param {string} raw nicht-leere Texteingabe
 * @returns {string|number|boolean} typisierter Wert oder der unveraenderte Text
 */
function coerceTypedInput(dtype, raw) {
  const text = String(raw).trim();
  if (isNumericType(dtype)) {
    const num = Number(text.replace(",", "."));
    if (text === "" || !Number.isFinite(num)) return raw;
    if (dtype === "INTEGER" && !Number.isInteger(num)) return raw;
    return num;
  }
  if (dtype === "BOOLEAN") {
    const lower = text.toLowerCase();
    if (["true", "ja", "1"].includes(lower)) return true;
    if (["false", "nein", "0"].includes(lower)) return false;
    return raw;
  }
  return raw;
}

/**
 * Zeigt ein Maskenfeld den Pflicht-Stern „*“?
 *
 * Ein Ankreuzfeld hat immer einen Wert (angehakt = ja, leer = nein); der Stern
 * versprach dort „muss angehakt sein“, ein Abschluss ohne Haken ging aber
 * durch. Deshalb gibt es bei Ankreuzfeldern keinen Stern, und der
 * Maskendesigner bietet „Pflicht“ dort nicht an. Gemeinsam fuer Vorschau des
 * Designers und Aufgabenmaske.
 * @param {{required?: boolean, widget?: string}} f Maskenfeld
 * @returns {boolean}
 */
function showsRequiredMark(f) {
  return !!f.required && f.widget !== "CHECKBOX";
}

/**
 * Widget fuer ein Datenelement **ohne** gestaltete Maske (Abschliessen ohne
 * Maske, Dialog „Instanzdaten eingeben"): nach Datentyp, nie ein Freitextfeld
 * fuer Zahl, Datum oder Ja/Nein. BOOLEAN bekommt eine Ja/Nein-Auswahl ohne
 * Vorbelegung (``YESNO``) statt eines Ankreuzfelds -- ein Ankreuzfeld liefert
 * immer einen Wert, ein uebersehenes Pflichtfeld waere dann still „Nein".
 * @param {object|undefined} elem Datenelement
 * @returns {string} Widget-Art fuer :func:`maskControl`
 */
function fallbackWidget(elem) {
  const dtype = elem ? elem.data_type : "STRING";
  if (dtype === "BOOLEAN") return "YESNO";
  if (isNumericType(dtype)) return "NUMBER";
  if (dtype === "DATE") return "DATE";
  return "TEXT";
}

// Widget-Factory: erzeugt fuer ein Datenelement + Widget-Typ das passende
// Eingabe-Control (control) samt Lesefunktion (read). Wird vom Eingabemasken-
// Designer, von der Laufzeit-Maske und vom Dialog „Instanzdaten eingeben"
// gemeinsam genutzt. ``read()`` liefert ``undefined`` fuer ein leeres Feld,
// sonst den typisierten Wert (``coerceTypedInput``) -- oder, wenn die Eingabe
// nicht eindeutig passt, den rohen Text, den der Kern mit D3 ablehnt.
// Zahlenfelder tragen ``step="any"``: ohne das markiert der Browser 499,99 als
// ungueltig, obwohl der Wert gespeichert wird.
function maskControl(elem, widget, options, current) {
  const dtype = elem ? elem.data_type : "STRING";
  const coerce = (raw) => coerceTypedInput(dtype, raw);
  if (widget === "YESNO") {
    const input = el("select", null,
      el("option", { value: "" }, "\u2013 bitte w\u00E4hlen \u2013"),
      el("option", { value: "true" }, "Ja"),
      el("option", { value: "false" }, "Nein"));
    if (current === true || current === false) input.value = String(current);
    return { control: input, read: () => (input.value === "" ? undefined : input.value === "true") };
  }
  if (widget === "CHECKBOX") {
    const input = el("input", { type: "checkbox" });
    if (current === true || current === "true" || current === "1") input.checked = true;
    return { control: input, read: () => input.checked };
  }
  if (widget === "TEXTAREA") {
    const input = el("textarea", { rows: "3", placeholder: elem ? elem.name : "" });
    if (current != null) input.value = String(current);
    return { control: input, read: () => (input.value === "" ? undefined : input.value) };
  }
  if (widget === "DROPDOWN") {
    const input = el("select", null,
      el("option", { value: "" }, "\u2013 bitte w\u00E4hlen \u2013"),
      ...(options || []).map((o) => el("option", { value: o }, o)));
    if (current != null) input.value = String(current);
    return { control: input, read: () => (input.value === "" ? undefined : input.value) };
  }
  const type = widget === "NUMBER" ? "number" : widget === "DATE" ? "date" : "text";
  const attrs = { type, placeholder: elem ? elem.name : "" };
  if (widget === "NUMBER") attrs.step = dtype === "INTEGER" ? "1" : dtype === "DECIMAL" ? "0.01" : "any";
  const input = el("input", attrs);
  if (current != null) input.value = String(current);
  return { control: input, read: () => (input.value === "" ? undefined : coerce(input.value)) };
}

/**
 * Pruefregeln eines Maskenfelds, soweit sie zum Bedienelement passen:
 * Unter-/Obergrenze fuer Zahlenfelder, Muster und Hoechstlaenge fuer Text.
 * Nur fuer Eingabefelder -- ein Anzeigefeld prueft nichts.
 * @param {object} f Feld des Designers
 * @returns {{min_value: (number|null), max_value: (number|null), pattern: (string|null), max_length: (number|null)}}
 */
function fieldRulesFor(f) {
  const writes = f.mode !== "READ";
  const num = writes && f.widget === "NUMBER";
  const text = writes && (f.widget === "TEXT" || f.widget === "TEXTAREA");
  const n = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? null : Number(v));
  return {
    min_value: num ? n(f.min_value) : null,
    max_value: num ? n(f.max_value) : null,
    pattern: text && f.pattern ? f.pattern : null,
    max_length: text ? n(f.max_length) : null,
  };
}

/**
 * Eingaben fuer die Pruefregeln eines Felds im Designer. Welche
 * erscheinen, haengt am Bedienelement; ob sie zusammenpassen (Grenzen in der
 * richtigen Reihenfolge, gueltiges Muster), prueft der Kern (U2).
 * @param {object} f Feld des Designers (wird direkt beschrieben)
 * @returns {HTMLElement[]} Zellen fuer die Feldzeile
 */
function fieldRuleCells(f) {
  if (f.mode === "READ") return [];
  const numInput = (key, placeholder) => {
    const i = el("input", { type: "number", step: "any", placeholder, value: f[key] ?? "" });
    i.addEventListener("input", () => { f[key] = i.value === "" ? null : Number(i.value); });
    return i;
  };
  if (f.widget === "NUMBER") {
    return [
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Mindestens"), numInput("min_value", "optional")),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "H\u00F6chstens"), numInput("max_value", "optional")),
    ];
  }
  if (f.widget === "TEXT" || f.widget === "TEXTAREA") {
    const pat = el("input", { type: "text", value: f.pattern || "", placeholder: "optional, z. B. [A-Z]{2}-\\d{4}" });
    pat.addEventListener("input", () => { f.pattern = pat.value.trim() || null; });
    return [
      el("div", { class: "fd-cell fd-wide" }, el("span", { class: "fd-cap" }, "Muster (regul\u00E4rer Ausdruck)"), pat),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "H\u00F6chstl\u00E4nge"), numInput("max_length", "optional")),
    ];
  }
  return [];
}

/**
 * Prueft einen Wert gegen die Pruefregeln eines Maskenfelds -- dieselben wie
 * der Kern beim Abschliessen (U4). Nur Komfort: Der Nutzer sieht das Problem
 * am Feld, bevor er absendet. Maßgeblich bleibt der Server.
 * @param {object} f Maskenfeld (FormField)
 * @param {*} val gelesener, typisierter Wert
 * @returns {string|null} deutscher Hinweis oder null
 */
function fieldRuleProblem(f, val) {
  if (typeof val === "number") {
    if (f.min_value != null && val < f.min_value) return `mindestens ${f.min_value}`;
    if (f.max_value != null && val > f.max_value) return `h\u00F6chstens ${f.max_value}`;
  }
  if (typeof val === "string") {
    if (f.max_length != null && val.length > f.max_length) return `h\u00F6chstens ${f.max_length} Zeichen`;
    if (f.pattern) {
      try { if (!new RegExp(`^(?:${f.pattern})$`).test(val)) return "passt nicht zum vorgegebenen Format"; }
      catch (e) { /* ungueltiges Muster: der Kern prueft */ }
    }
  }
  return null;
}

/**
 * Markiert ein Feld der Aufgabenmaske als fehlerhaft (oder hebt die Markierung
 * auf). Pflicht- und Regelfehler standen bisher nur in einer Meldung, nicht am
 * Feld.
 * @param {HTMLElement|undefined} wrap das ``label.field`` des Felds
 * @param {string|null} message Hinweis oder null zum Aufheben
 */
function markField(wrap, message) {
  if (!wrap) return;
  const old = wrap.querySelector(".field-error-msg");
  if (old) old.remove();
  wrap.classList.toggle("field-invalid", !!message);
  const control = wrap.querySelector("input, select, textarea");
  if (control) {
    if (message) control.setAttribute("aria-invalid", "true");
    else control.removeAttribute("aria-invalid");
  }
  if (message) wrap.appendChild(el("span", { class: "field-error-msg" }, message));
}

// Visueller Eingabemasken-Designer: Felder per Auswahl zusammenstellen; die
// Anordnung entsteht automatisch (geordnete Liste -> Grid). Jedes Feld wird auf
// einen Datenzugriff abgebildet, daher gilt Correctness by Construction (der
// Kern lehnt u.a. jedes Lesefeld ohne vorheriges Schreiben ab -- D1).
function openFormDesigner(nodeId) {
  const schema = state.schema;
  const elements = Object.values(schema.data_elements || {});
  if (!elements.length) {
    toast("info", "Zuerst Datenelemente in der Datensicht anlegen.");
    return;
  }
  const existing = (schema.forms || {})[nodeId];
  let title = existing ? existing.title : "";
  let columns = existing ? existing.columns || 1 : 1;
  // help_text und group werden mitgefuehrt: Frueher fehlte help_text hier, und
  // ein blosses Oeffnen + Speichern loeschte die Hilfetexte einer Maske.
  const fields = existing
    ? existing.fields.map((f) => ({
        element_id: f.element_id, widget: f.widget, label: f.label,
        mode: f.mode, required: f.required, options: (f.options || []).slice(),
        help_text: f.help_text || null, group: f.group || "",
        // Pruefregeln mitfuehren -- sonst loeschte Oeffnen + Speichern sie.
        min_value: f.min_value ?? null, max_value: f.max_value ?? null,
        pattern: f.pattern || null, max_length: f.max_length ?? null,
      }))
    : [];
  const container = el("div", { class: "form-designer" });

  const defaultField = () => {
    const elem = elements[0];
    return {
      element_id: elem.id, widget: WIDGETS_FOR_TYPE[elem.data_type][0],
      label: elem.name, mode: "WRITE", required: true, options: [], help_text: null, group: "",
    };
  };

  function previewMask() {
    if (!fields.length) return el("div", { class: "muted", style: "font-size:12px" }, "Noch keine Felder.");
    return maskLayout(fields.map((f) => {
      const elem = schema.data_elements[f.element_id];
      const { control } = maskControl(elem, f.widget, f.options, null);
      control.setAttribute("disabled", "disabled");
      return { group: f.group, node: el("label", { class: "field" },
        (f.label || (elem ? elem.name : f.element_id)) + (showsRequiredMark(f) ? " *" : ""), control) };
    }), columns);
  }

  function fieldRow(f, idx) {
    const elem = schema.data_elements[f.element_id];
    const elemSel = el("select", null,
      ...elements.map((e) => el("option", { value: e.id }, `${e.name} (${typeName(e.data_type)})`)));
    elemSel.value = f.element_id;
    elemSel.addEventListener("change", () => {
      const prev = schema.data_elements[f.element_id];
      f.element_id = elemSel.value;
      const next = schema.data_elements[f.element_id];
      // Keep the label in sync while it is still the untouched default.
      if (!f.label || (prev && f.label === prev.name)) f.label = next.name;
      if (!WIDGETS_FOR_TYPE[next.data_type].includes(f.widget)) f.widget = WIDGETS_FOR_TYPE[next.data_type][0];
      if (f.widget !== "DROPDOWN") f.options = [];
      renderDesigner();
    });
    const allowed = elem ? WIDGETS_FOR_TYPE[elem.data_type] : ["TEXT"];
    const widgetSel = el("select", null,
      ...allowed.map((w) => el("option", { value: w }, WIDGET_LABELS[w])));
    widgetSel.value = f.widget;
    widgetSel.addEventListener("change", () => {
      f.widget = widgetSel.value;
      if (f.widget !== "DROPDOWN") f.options = [];
      renderDesigner();
    });
    const labelInput = el("input", { type: "text", value: f.label });
    labelInput.addEventListener("input", () => { f.label = labelInput.value; });
    const modeSel = el("select", null,
      el("option", { value: "WRITE" }, "Eingabe (schreibt)"),
      el("option", { value: "READ" }, "Anzeige (liest)"));
    modeSel.value = f.mode;
    modeSel.addEventListener("change", () => { f.mode = modeSel.value; renderDesigner(); });
    const reqBox = el("input", { type: "checkbox" });
    reqBox.checked = f.required;
    reqBox.addEventListener("change", () => { f.required = reqBox.checked; });
    const groupInput = el("input", { type: "text", value: f.group || "", placeholder: "optional" });
    // Nur die Vorschau erneuern: ``change`` feuert beim Verlassen des Felds --
    // also schon beim Druecken auf „+ Feld hinzufuegen“. Ein Neuaufbau des
    // ganzen Designers ersetzte den Knopf dann vor dem Loslassen, und der
    // erste Klick ging verloren.
    groupInput.addEventListener("input", () => { f.group = groupInput.value.trim(); });
    groupInput.addEventListener("change", () => { f.group = groupInput.value.trim(); refreshPreview(); });

    const cells = [
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Datenelement"), elemSel),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Darstellung"), widgetSel),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Beschriftung"), labelInput),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Richtung"), modeSel),
      el("div", { class: "fd-cell" }, el("span", { class: "fd-cap" }, "Gruppe"), groupInput),
      // Ein Ankreuzfeld hat immer einen Wert -- „Pflicht“ waere wirkungslos
      // (siehe ``showsRequiredMark``) und wird deshalb nicht angeboten.
      f.widget === "CHECKBOX"
        ? el("div", { class: "fd-cell fd-req muted", style: "font-size:12px" },
            "Ja/Nein hat immer einen Wert")
        : el("div", { class: "fd-cell fd-req" },
            el("label", { class: "row", style: "gap:6px;align-items:center" }, reqBox, "Pflicht")),
    ];
    if (f.widget === "DROPDOWN") {
      const optInput = el("input", {
        type: "text", value: (f.options || []).join(", "), placeholder: "Option A, Option B",
      });
      optInput.addEventListener("input", () => {
        f.options = optInput.value.split(",").map((s) => s.trim()).filter((s) => s.length);
      });
      cells.push(el("div", { class: "fd-cell fd-wide" },
        el("span", { class: "fd-cap" }, "Optionen (kommagetrennt)"), optInput));
    }
    cells.push(...fieldRuleCells(f));
    cells.push(el("button", { class: "btn small danger fd-del", onClick: () => { fields.splice(idx, 1); renderDesigner(); } }, "Entfernen"));
    return el("div", { class: "fd-field" }, ...cells);
  }

  function renderDesigner() {
    clear(container);
    const titleInput = el("input", { type: "text", value: title, placeholder: "Titel der Maske (optional)" });
    titleInput.addEventListener("input", () => { title = titleInput.value; });
    container.appendChild(el("label", { class: "field" }, "Maskentitel", titleInput));
    const colSel = el("select", null,
      ...[1, 2, 3].map((n) => el("option", { value: String(n) }, n === 1 ? "1 Spalte" : `${n} Spalten`)));
    colSel.value = String(columns);
    colSel.addEventListener("change", () => { columns = Number(colSel.value); renderDesigner(); });
    container.appendChild(el("label", { class: "field" }, "Anordnung (am Smartphone immer einspaltig)", colSel));

    const list = el("div", { class: "fd-list" });
    fields.forEach((f, idx) => list.appendChild(fieldRow(f, idx)));
    container.appendChild(list);

    container.appendChild(el("button", {
      class: "btn small", onClick: () => { fields.push(defaultField()); renderDesigner(); },
    }, "+ Feld hinzuf\u00FCgen"));

    previewBox = el("div", { class: "fd-preview" },
      el("div", { class: "fd-preview-h" }, "Vorschau"),
      previewMask());
    container.appendChild(previewBox);
  }

  // Erneuert nur die Vorschau -- die Eingabefelder (und Knoepfe) bleiben, wo
  // sie sind; siehe Gruppe in fieldRow.
  let previewBox = null;
  function refreshPreview() {
    if (!previewBox) return;
    clear(previewBox);
    previewBox.appendChild(el("div", { class: "fd-preview-h" }, "Vorschau"));
    previewBox.appendChild(previewMask());
  }

  renderDesigner();
  openModal(existing ? "Eingabemaske bearbeiten" : "Eingabemaske gestalten", container, async () => {
    if (!fields.length) { toast("err", "Mindestens ein Feld ist erforderlich."); return false; }
    const payload = {
      title,
      columns,
      fields: fields.map((f) => ({
        element_id: f.element_id, widget: f.widget, label: f.label,
        mode: f.mode, required: f.required,
        options: f.widget === "DROPDOWN" ? f.options : [],
        help_text: f.help_text || null, group: f.group || "",
        // Pruefregeln nur, wo sie zum Bedienelement passen (sonst U2).
        ...fieldRulesFor(f),
      })),
    };
    try {
      await api.post(`/schemas/${state.schemaId}/nodes/${nodeId}/form`, payload);
      await refreshSchema();
      render();
      toast("ok", "Eingabemaske gespeichert");
    } catch (err) { toastError(err); return false; }
  }, "Speichern");
}

function validationBadge() {
  if (!state.validation) return el("span", null, "");
  if (state.validation.correct) return el("span", { class: "pill pill-green" }, "korrekt");
  return el("span", { class: "pill pill-red" }, countLabel(state.validation.findings.length, "Befund", "Befunde"));
}

function findingsPanel() {
  const v = state.validation;
  const body = el("div", { class: "panel-b" });
  if (!v || v.correct) {
    body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Strukturell korrekt (K/D/Z/A/C/H/F/U erf\u00FCllt)."));
  } else {
    v.findings.forEach((f) => body.appendChild(el("div", { class: "finding" },
      el("span", { class: "rule" }, f.rule),
      el("span", null, findingText(f, { withHint: true })))));
  }
  // Freigabereife (Stufe B) wie in der Statusleiste der Karten-Sicht: ein
  // Entwurf darf unfertig sein, soll es aber vor der Freigabe sehen. Dieselbe
  // Quelle (releaseFindings) -- beide Oberflaechen zeigen dasselbe.
  const notReady = isDraft(state.schema) ? releaseFindings() : [];
  if (notReady.length) {
    body.appendChild(el("div", { class: "warn-banner", style: "margin-top:8px" },
      `${countLabel(notReady.length, "Schritt", "Schritte")} ohne Bearbeiter \u2013 erst danach l\u00E4sst sich freigeben.`));
    notReady.forEach((f) => body.appendChild(el("div", { class: "finding" },
      el("span", { class: "rule" }, f.rule),
      el("span", null, findingText(f, { withHint: true })))));
  }
  // Modellhinweise (G-Gruppe, /metrics): beratend, kein Korrektheitsurteil.
  // Bewusst im selben Panel, aber klar abgesetzt \u2013 ein Hinweis ist kein Befund.
  const hints = state.hints || [];
  if (hints.length) {
    body.appendChild(el("div", { class: "hr" }));
    body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-bottom:6px" },
      "Hinweise (beratend, blockieren nichts):"));
    hints.forEach((h) => body.appendChild(el("div", { class: "finding" },
      el("span", { class: "rule rule-hint" }, h.code),
      // Anklickbar: waehlt den betroffenen Schritt im Kontrollfluss aus --
      // so findet man auch gleichartige, unbenannte Knoten.
      h.node_id && state.schema && state.schema.nodes[h.node_id]
        ? el("a", { href: "#", title: "Im Kontrollfluss zeigen",
            onClick: (e) => { e.preventDefault(); state.selectedNode = h.node_id; render(); } },
            hintText(h, state.schema))
        : el("span", null, hintText(h, state.schema)))));
  }
  return el("div", { class: "panel", "data-tour": "model.findings" },
    el("div", { class: "panel-h" }, el("h2", null, "Korrektheit"), el("span", { class: "sub" }, "live vom Kern")), body);
}

/** Grund fuer jede Sperre, die nur ein Entwurf aufhebt (siehe ``lockedBy``). */
const DRAFT_ONLY_REASON = "Freigegeben \u2013 \u00E4nderbar nur in einer neuen Revision (Modellieren \u2192 \u201ENeue Revision\u201C).";

/**
 * Sperre eines Knopfs **mit Grund**: liefert ``disabled`` und den Grund als
 * ``title`` (Tooltip) fuer ``el()``.
 *
 * Gesperrte Knoepfe ohne Grund (freigegebenes Schema, fehlender Connector)
 * liessen raten, warum nichts geht. Jede Sperre nennt jetzt die erste
 * zutreffende Bedingung.
 * @param {...Array} reasons Paare ``[Bedingung, Grund]`` in Prioritaetsfolge
 * @returns {{disabled?: boolean, title?: string}} leeres Objekt, wenn nichts
 *   sperrt -- zum Einspreizen in die Attribute (``...lockedBy(...)``)
 */
function lockedBy(...reasons) {
  const hit = reasons.find(([cond]) => cond);
  return hit ? { disabled: true, title: hit[1] } : {};
}

function revisionPanel() {
  const schema = state.schema;
  if (isDraft(schema)) return el("div");
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Schema-Evolution")),
    el("div", { class: "panel-b row" },
      el("span", { class: "muted", style: "font-size:12px;flex:1" }, "Eine neue Revision erzeugt eine bearbeitbare Entwurfskopie als neue Version; die Schritte behalten ihre Kennungen, damit laufende Vorgänge sich übernehmen lassen."),
      el("button", { class: "btn small", onClick: newRevision }, "Neue Revision")));
}

/** Vorbelegte Hoechstzahl der Schleifen-Durchlaeufe im Einfuege-Dialog. */
const LOOP_MAX_DEFAULT = 10;

/** Wert der Auswahl „Neues Merkmal anlegen …“ in ``discriminatorPicker``. */
const NEW_DISCRIMINATOR = "__new__";

/**
 * Auswahl des steuernden Merkmals mit „＋ Neues Merkmal anlegen …“.
 *
 * Gemeinsam fuer die Entscheidung (XOR) und die Schleife im Einfuegedialog --
 * beide Modellier-Oberflaechen oeffnen denselben Dialog. Frueher bot nur die
 * Entscheidung die Neuanlage; wer eine Schleife ueber ein noch nicht
 * vorhandenes Ja/Nein-Merkmal bauen wollte, musste den Dialog abbrechen.
 * Angelegt wird erst beim Bestaetigen (``withDiscriminator``); ob das Merkmal
 * passt, entscheidet der Kern.
 *
 * @param {object[]} elements waehlbare vorhandene Datenelemente
 * @param {Array<[string, string]>} newTypes Typen fuer ein neues Merkmal als
 *   [Datentyp, Beschriftung]; der erste ist vorgeschlagen
 * @param {string} cls CSS-Klasse der Auswahl (Tests, Tour)
 * @param {string} placeholder Beispielname im Namensfeld
 * @returns {{select: object, box: object, nameInput: object, typeSelect: object,
 *   isNew: function(): boolean, dataType: function(): (string|undefined),
 *   choice: function(): {existing: (string|null), newName: string, newType: string}}}
 *   ``box`` (Name und Art) ist nur bei „Neues Merkmal“ sichtbar. Ohne
 *   vorhandene Elemente ist „Neues Merkmal“ vorgewaehlt. ``dataType`` liefert
 *   den Typ der aktuellen Wahl (bei Neuanlage den gewaehlten Typ).
 */
function discriminatorPicker(elements, newTypes, cls, placeholder) {
  const select = el("select", { class: cls },
    ...elements.map((d) => el("option", { value: d.id }, `${d.name} (${typeName(d.data_type)})`)),
    el("option", { value: NEW_DISCRIMINATOR }, "\uFF0B Neues Merkmal anlegen \u2026"));
  const nameInput = el("input", { type: "text", placeholder });
  const typeSelect = el("select", null, ...newTypes.map(([v, label]) => el("option", { value: v }, label)));
  const box = el("div", { class: "form-grid new-disc" },
    el("label", { class: "field" }, "Name des Merkmals", nameInput),
    el("label", { class: "field" }, "Art", typeSelect));
  if (!elements.length) select.value = NEW_DISCRIMINATOR;
  const isNew = () => select.value === NEW_DISCRIMINATOR;
  const sync = () => { box.style.display = isNew() ? "" : "none"; };
  select.addEventListener("change", sync);
  sync();
  return {
    select, box, nameInput, typeSelect, isNew,
    dataType: () => (isNew() ? typeSelect.value : (state.schema.data_elements[select.value] || {}).data_type),
    choice: () => ({ existing: isNew() ? null : select.value, newName: nameInput.value.trim(), newType: typeSelect.value }),
  };
}

/**
 * Fuehrt eine Einfuege-Operation mit ihrem Merkmal aus und legt es bei Bedarf
 * vorher an.
 *
 * Neuanlage ist eine eigene Kern-Operation (``POST …/data-elements``), danach
 * laeuft ``run`` mit der Kennung. Scheitert ``run``, wird das hier angelegte
 * Element wieder geloescht -- kein halbes Merkmal bleibt im Modell; die
 * Meldung des Kerns wird weitergereicht.
 *
 * @param {string} sid Schema
 * @param {{existing: (string|null), newName: string, newType: string}} opt
 *   ``existing`` = vorhandenes Element; sonst Name und Typ des neuen
 * @param {function(string): Promise<*>} run Operation mit der Merkmal-Kennung
 * @returns {Promise<*>} Ergebnis von ``run``
 * @throws {{detail: string}} ohne Namen fuer ein neues Merkmal; sonst den
 *   Fehler der Anlage bzw. von ``run``
 */
async function withDiscriminator(sid, opt, run) {
  let created = null;
  try {
    let disc = opt.existing;
    if (!disc) {
      if (!opt.newName) throw { detail: "Bitte einen Namen f\u00FCr das neue Merkmal angeben." };
      const before = new Set(Object.keys(state.schema.data_elements || {}));
      const schema = await api.post(`/schemas/${sid}/data-elements`, { name: opt.newName, data_type: opt.newType });
      created = Object.values(schema.data_elements || {}).find((d) => !before.has(d.id)) || null;
      if (!created) throw { detail: "Das neue Merkmal wurde nicht angelegt." };
      disc = created.id;
    }
    return await run(disc);
  } catch (err) {
    // Aufraeumen; ein Fehler dabei wird verschluckt, die eigentliche Ursache
    // ist die Meldung von oben.
    if (created) { try { await api.del(`/schemas/${sid}/data-elements/${created.id}`); } catch (e) { /* egal */ } }
    throw err;
  }
}

/**
 * Einfuegedialog hinter einem Knoten bzw. auf einer Kante.
 *
 * @param {string} afterNodeId Quelle der Kante (Anker)
 * @param {string} [beforeNodeId] Ziel der Kante. Nur noetig, wenn der Anker
 *   mehrere Ausgaenge hat (Anfang eines Zweigs, auch eines leeren): dann wird
 *   genau auf dieser Kante eingefuegt, und angeboten wird nur ein einzelner
 *   Schritt -- Verzweigung oder Schleife direkt am Zweiganfang kann der Kern
 *   (noch) nicht.
 */
function openInsertModal(afterNodeId, beforeNodeId) {
  const node = state.schema.nodes[afterNodeId];
  const exits = controlEdges(state.schema).filter((e) => e.source === afterNodeId);
  const branchStart = exits.length > 1 && typeof beforeNodeId === "string" ? beforeNodeId : null;
  let active = "serial";
  const serialBody = el("label", { class: "field" }, "Bezeichnung",
    el("input", { type: "text", id: "ins-label", placeholder: "z. B. Antrag pr\u00FCfen" }));
  const parBox = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:8px" });
  function addParRow(val) {
    parBox.appendChild(el("input", { type: "text", class: "par-branch", placeholder: "Zweig-Bezeichnung", value: val || "" }));
  }
  // --- XOR partition builder (K7): a typed discriminator drives the branches.
  const partitionable = Object.values(state.schema.data_elements).filter(
    (d) => d.source === "INSTANCE" && ["INTEGER", "FLOAT", "DECIMAL", "BOOLEAN", "STRING"].includes(d.data_type));
  // Das Merkmal muss nicht vorher woanders angelegt UND an einem Schritt
  // davor geschrieben werden, sonst schickte der Dialog den Nutzer weg. Die Wahl
  // „Neues Merkmal …“ legt es hier an, und der Schritt vor der Einfuegestelle
  // bekommt die Schreibbindung gleich mit (``writerStep``). Ob das alles
  // zusammen korrekt ist (D1, D2, K7), entscheidet weiterhin der Kern.
  const writerStep = node && node.type === "ACTIVITY" ? node : null;
  const condPick = discriminatorPicker(partitionable, [
    ["FLOAT", "Zahl (Stufen nach Grenzwerten)"],
    ["DECIMAL", "Betrag (Stufen nach Grenzwerten)"],
    ["BOOLEAN", "Ja/Nein"],
    ["STRING", "Text (Zweige nach Werten)"],
  ], "cond-disc", "z. B. Betrag");
  const condDisc = condPick.select;
  const bindBox = el("input", { type: "checkbox" });
  const bindRow = writerStep
    ? el("label", { class: "row", style: "gap:8px;align-items:center;font-size:12px" }, bindBox,
        `„${nodeCaption(writerStep)}“ setzt dieses Merkmal (Schreibbindung ergänzen)`)
    : el("div", { class: "muted", style: "font-size:12px" },
        "Vor dieser Stelle liegt kein Aufgaben-Schritt, der das Merkmal setzen könnte – es muss schon früher im Ablauf geschrieben werden.");
  function writesAlready(eid) {
    return !!writerStep && (state.schema.data_accesses || []).some((a) =>
      a.node_id === writerStep.id && a.element_id === eid && (a.mode === "WRITE" || a.mode === "READ_WRITE"));
  }
  function syncDiscChoice() {
    const isNew = condPick.isNew();
    // Vorbelegung: binden, wenn der Schritt davor das Merkmal noch nicht schreibt.
    bindBox.checked = isNew || !writesAlready(condDisc.value);
    bindBox.disabled = isNew;  // ein neues Merkmal braucht einen Schreiber
  }
  const condRows = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:8px" });
  function discKind() {
    const type = condPick.dataType();
    if (isNumericType(type)) return "THRESHOLD";
    if (type === "BOOLEAN") return "BOOLEAN";
    if (type === "STRING") return "ENUM";
    return null;
  }
  function addThresholdRow(last) {
    condRows.appendChild(el("div", { class: "branch-row threshold-row" },
      el("input", { type: "text", class: "cond-label", placeholder: "Bezeichnung" }),
      el("input", { type: "number", class: "cond-upper", placeholder: last ? "Obergrenze leer = bis +\u221E" : "unter \u2026" })));
  }
  function addEnumRow() {
    condRows.appendChild(el("div", { class: "branch-row enum-row" },
      el("input", { type: "text", class: "cond-label", placeholder: "Bezeichnung" }),
      el("input", { type: "text", class: "cond-values", placeholder: "Werte, kommagetrennt" })));
  }
  function rebuildCondRows() {
    clear(condRows);
    const kind = discKind();
    if (kind === "THRESHOLD") { addThresholdRow(false); addThresholdRow(true); }
    else if (kind === "BOOLEAN") {
      condRows.appendChild(el("div", { class: "branch-row bool-row" },
        el("span", { class: "muted" }, "wahr"),
        el("input", { type: "text", class: "cond-label", "data-bool": "true", placeholder: "Bezeichnung" })));
      condRows.appendChild(el("div", { class: "branch-row bool-row" },
        el("span", { class: "muted" }, "falsch"),
        el("input", { type: "text", class: "cond-label", "data-bool": "false", placeholder: "Bezeichnung" })));
    } else if (kind === "ENUM") {
      addEnumRow(); addEnumRow();
      condRows.appendChild(el("div", { class: "branch-row else-row" },
        el("span", { class: "muted" }, "Sonst (otherwise)"),
        el("input", { type: "text", class: "cond-label", "data-else": "1", placeholder: "Bezeichnung" })));
    }
  }
  function addCondRow() {
    const kind = discKind();
    if (kind === "THRESHOLD") {
      const rows = condRows.querySelectorAll(".threshold-row");
      addThresholdRow(false);
      if (rows.length) condRows.insertBefore(condRows.lastChild, rows[rows.length - 1]);
    } else if (kind === "ENUM") {
      const elseRow = condRows.querySelector(".else-row");
      addEnumRow();
      if (elseRow) condRows.insertBefore(condRows.lastChild, elseRow);
    }
  }
  condDisc.addEventListener("change", () => { syncDiscChoice(); rebuildCondRows(); });
  condPick.typeSelect.addEventListener("change", rebuildCondRows);
  addParRow(); addParRow(); syncDiscChoice(); rebuildCondRows();
  const condPanel = el("div", null,
    el("label", { class: "field" }, "Entscheiden nach (Merkmal)", condDisc),
    condPick.box,
    bindRow,
    el("div", { class: "muted", style: "font-size:12px;margin:4px 0" }, "Die Engine w\u00E4hlt den Zweig automatisch anhand des Werts \u2013 vollst\u00E4ndig und \u00FCberschneidungsfrei (K7)."),
    condRows, el("button", { class: "btn small ghost", onClick: () => addCondRow() }, "+ Zweig"));
  // --- Schleife (K6): Rumpf-Bezeichnung + entscheidbares Wiederholen-Merkmal.
  // BOOLEAN nutzt die Kurzform (repeat_value); Zahlen (Schwelle) und Text
  // (Wertemenge) bauen eine Wiederhol/Verlassen-Partition (Stufe S3, K6b).
  // Die Rumpfaktivität erhält vom Kern automatisch den Pflicht-Schreibzugriff
  // auf das Merkmal (K6c: jede Iteration entscheidet auf frischen Daten).
  const loopable = Object.values(state.schema.data_elements).filter(
    (d) => d.source === "INSTANCE" && ["BOOLEAN", "INTEGER", "FLOAT", "DECIMAL", "STRING"].includes(d.data_type));
  // Neues Merkmal wie bei der Entscheidung (gemeinsame Auswahl); Ja/Nein ist
  // vorgeschlagen, wie die Anleitung es empfiehlt.
  const loopPick = discriminatorPicker(loopable, [
    ["BOOLEAN", "Ja/Nein (empfohlen)"],
    ["INTEGER", "Zahl (Wiederholen ab/unter einer Grenze)"],
    ["STRING", "Text (Wiederholen bei bestimmten Werten)"],
  ], "loop-disc", "z. B. Nacharbeit n\u00F6tig");
  const loopDisc = loopPick.select;
  const loopRepeat = el("select", null,
    el("option", { value: "true" }, "wahr"),
    el("option", { value: "false" }, "falsch"));
  const loopCmp = el("select", { class: "loop-cmp" },
    el("option", { value: "gte" }, "größer/gleich der Grenze ist (≥)"),
    el("option", { value: "lt" }, "unter der Grenze liegt (<)"));
  const loopBound = el("input", { type: "number", class: "loop-bound", placeholder: "z. B. 1" });
  const loopValues = el("input", { type: "text", class: "loop-values", placeholder: "Werte, kommagetrennt – z. B. nacharbeit" });
  const loopRows = el("div", { class: "row", style: "flex-direction:column;align-items:stretch;gap:8px" });
  function loopKind() {
    const type = loopPick.dataType();
    if (!type) return null;
    if (type === "BOOLEAN") return "BOOLEAN";
    if (type === "STRING") return "ENUM";
    return "THRESHOLD";
  }
  function rebuildLoopRows() {
    clear(loopRows);
    const kind = loopKind();
    if (kind === "BOOLEAN") {
      loopRows.appendChild(el("label", { class: "field" }, "Wiederholen, solange der Wert", loopRepeat));
    } else if (kind === "THRESHOLD") {
      loopRows.appendChild(el("label", { class: "field" }, "Wiederholen, solange der Wert", loopCmp));
      loopRows.appendChild(el("label", { class: "field" }, "Grenze", loopBound));
    } else if (kind === "ENUM") {
      loopRows.appendChild(el("label", { class: "field" },
        "Wiederholen bei diesen Werten (alle anderen verlassen die Schleife)", loopValues));
    }
  }
  loopDisc.addEventListener("change", rebuildLoopRows);
  loopPick.typeSelect.addEventListener("change", rebuildLoopRows);
  rebuildLoopRows();
  // Optionale Notbremse (S3): Höchstzahl der Durchläufe. Deterministisch –
  // am Limit wird verlassen, auch wenn die Daten „wiederholen“ sagen; die
  // Zeitprüfung (T2) rechnet den Rumpf dann entsprechend oft.
  // Vorbelegt mit LOOP_MAX_DEFAULT: eine Schleife
  // ohne Obergrenze ist moeglich, soll aber eine bewusste Entscheidung sein
  // und keine Voreinstellung. Pflicht ist sie nicht -- das waere eine
  // Verschaerfung von K6b fuer den gesamten Bestand.
  const loopMax = el("input", { type: "number", class: "loop-max", min: "2",
    value: String(LOOP_MAX_DEFAULT), placeholder: "leer = unbegrenzt" });
  const loopPanel = el("div", null,
    el("label", { class: "field" }, "Bezeichnung des Wiederhol-Schritts",
      el("input", { type: "text", id: "loop-label", placeholder: "z. B. Nacharbeit erledigen" })),
    el("label", { class: "field" }, "Wiederholen-Merkmal (Datenelement)", loopDisc),
    loopPick.box,
    loopRows,
    el("label", { class: "field" }, "Höchstzahl Durchläufe (Notbremse, mind. 2 – leeren nur, wenn die Schleife wirklich unbegrenzt laufen darf)", loopMax),
    el("div", { class: "muted", style: "font-size:12px;margin:4px 0" },
      "Der Schritt läuft mindestens einmal; am Ende jeder Runde entscheidet das Merkmal automatisch, ob wiederholt wird (K6). Der Schritt schreibt das Merkmal verbindlich – jede Runde entscheidet auf frischen Daten."));
  const panels = {
    serial: serialBody,
    parallel: el("div", null, parBox, el("button", { class: "btn small ghost", onClick: () => addParRow() }, "+ Zweig")),
    conditional: condPanel,
    loop: loopPanel,
  };
  const slot = el("div", null, panels.serial);
  const tabs = branchStart
    ? el("div", { class: "tabs" }, tabBtn("Seriell", "serial", true))
    : el("div", { class: "tabs" },
        tabBtn("Seriell", "serial", true), tabBtn("Parallel (UND)", "parallel"), tabBtn("Bedingt (XOR)", "conditional"), tabBtn("Schleife", "loop"));
  function tabBtn(label, key, isActive) {
    return el("button", { class: isActive ? "active" : "", onClick: (e) => {
      active = key;
      [...tabs.children].forEach((c) => c.classList.remove("active"));
      e.target.classList.add("active");
      clear(slot); slot.appendChild(panels[key]);
    } }, label);
  }
  // Keine Variante ist mehr gesperrt: Entscheidung und Schleife legen ein
  // fehlendes Merkmal selbst an (``discriminatorPicker``), statt den Nutzer
  // aus dem Dialog in die Datensicht zu schicken.
  const body = el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" }, branchStart
      ? `Einf\u00FCgen am Anfang des Zweigs: ${branchCaption(state.schema, afterNodeId, branchStart)}`
      : `Einf\u00FCgen nach: ${nodeCaption(node)}`),
    tabs, slot);

  openModal("Schritt einf\u00FCgen", body, async () => {
    try {
      if (active === "serial") {
        const label = byId("ins-label").value.trim();
        if (!label) return false;
        const req = { label, after_node_id: afterNodeId };
        if (branchStart) req.before_node_id = branchStart;
        await api.post(`/schemas/${state.schemaId}/serial-insert`, req);
      } else if (active === "parallel") {
        const labels = [...parBox.querySelectorAll(".par-branch")].map((i) => i.value.trim()).filter(Boolean);
        if (labels.length < 2) { toast("err", "Mindestens zwei Zweige n\u00F6tig"); return false; }
        await api.post(`/schemas/${state.schemaId}/parallel-insert`, { branch_labels: labels, after_node_id: afterNodeId });
      } else if (active === "loop") {
        const label = byId("loop-label") ? byId("loop-label").value.trim() : "";
        if (!label) { toast("err", "Bezeichnung des Wiederhol-Schritts fehlt"); return false; }
        const kind = loopKind();
        const payload = { label, after_node_id: afterNodeId };
        if (kind === "BOOLEAN") {
          payload.repeat_value = loopRepeat.value === "true";
        } else if (kind === "THRESHOLD") {
          // Zwei Zellen kacheln die Zahlengerade: [-\u221E, G) und [G, +\u221E); die
          // Vergleichswahl bestimmt, welche Seite wiederholt (K6b: je eine
          // Wiederhol- und eine Verlassen-Zelle).
          const bound = loopBound.value.trim();
          if (loopBound.validity && loopBound.validity.badInput) { toast("err", "Grenze ist keine Zahl"); return false; }
          if (bound === "") { toast("err", "Grenze fehlt"); return false; }
          const g = Number(bound);
          payload.cells = loopCmp.value === "gte"
            ? [{ repeat: false, upper: g }, { repeat: true }]
            : [{ repeat: true, upper: g }, { repeat: false }];
        } else {
          const values = loopValues.value.split(",").map((v) => v.trim()).filter(Boolean);
          if (!values.length) { toast("err", "Mindestens ein Wiederhol-Wert n\u00F6tig"); return false; }
          payload.cells = [{ repeat: true, values }, { repeat: false, is_else: true }];
        }
        if (loopMax.value.trim() !== "") {
          const m = Number(loopMax.value);
          if (!Number.isInteger(m) || m < 2) { toast("err", "H\u00F6chstzahl Durchl\u00E4ufe: mindestens 2"); return false; }
          payload.max_iterations = m;
        }
        const sid = state.schemaId;
        await withDiscriminator(sid, loopPick.choice(),
          (disc) => api.post(`/schemas/${sid}/loop-insert`, { ...payload, discriminator: disc }));
      } else {
        const kind = discKind();
        if (!kind) { toast("err", "Kein Entscheidungs-Datenelement gew\u00E4hlt", ["Bitte oben ein Datenelement ausw\u00E4hlen, nach dem verzweigt wird."]); return false; }
        let branches = [];
        if (kind === "THRESHOLD") {
          // Ein Zahlenfeld liefert fuer „abc“ einen leeren Wert -- das galt
          // bisher still als „ohne Obergrenze“ und endete in einer
          // irrefuehrenden Meldung. ``badInput`` verraet die Eingabe.
          const bad = [...condRows.querySelectorAll(".threshold-row")].filter((r) =>
            r.querySelector(".cond-upper").validity && r.querySelector(".cond-upper").validity.badInput);
          bad.forEach((r) => markField(r, "keine Zahl"));
          if (bad.length) { toast("err", "Grenzwert ist keine Zahl", ["Bitte eine Zahl eingeben oder das Feld für die oberste Stufe leer lassen."]); return false; }
          const rows = [...condRows.querySelectorAll(".threshold-row")].map((r) => ({
            label: r.querySelector(".cond-label").value.trim(),
            upperRaw: r.querySelector(".cond-upper").value.trim(),
          })).filter((b) => b.label);
          if (rows.length < 2) { toast("err", "Mindestens zwei Stufen n\u00F6tig"); return false; }
          const unbounded = rows.filter((b) => b.upperRaw === "");
          if (unbounded.length !== 1) { toast("err", "Genau eine Stufe muss ohne Obergrenze (bis +\u221E) sein"); return false; }
          const bounded = rows.filter((b) => b.upperRaw !== "")
            .map((b) => ({ label: b.label, upper: Number(b.upperRaw) }))
            .sort((a, b) => a.upper - b.upper);
          branches = [...bounded, { label: unbounded[0].label }];
        } else if (kind === "BOOLEAN") {
          branches = [...condRows.querySelectorAll(".cond-label")]
            .map((i) => ({ label: i.value.trim(), bool_value: i.dataset.bool === "true" }))
            .filter((b) => b.label);
          if (branches.length !== 2) { toast("err", "Beide F\u00E4lle (wahr/falsch) ben\u00F6tigen eine Bezeichnung"); return false; }
        } else {
          branches = [...condRows.children].map((r) => {
            const labelEl = r.querySelector(".cond-label");
            const valuesEl = r.querySelector(".cond-values");
            if (!labelEl || !labelEl.value.trim()) return null;
            if (labelEl.dataset.else) return { label: labelEl.value.trim(), is_else: true };
            const values = (valuesEl ? valuesEl.value : "").split(",").map((v) => v.trim()).filter(Boolean);
            if (!values.length) return null;
            return { label: labelEl.value.trim(), values };
          }).filter(Boolean);
          if (branches.length < 2) { toast("err", "Mindestens ein Wertzweig plus Sonst-Zweig n\u00F6tig"); return false; }
        }
        await insertConditionalWithDiscriminator(afterNodeId, branches, {
          ...condPick.choice(),
          bindAt: writerStep && bindBox.checked ? writerStep.id : null,
        });
      }
      await refreshSchema();
      render();
      toast("ok", "Schritt eingef\u00FCgt");
    } catch (err) { toastError(err); return false; }
  }, "Einf\u00FCgen");
}

/**
 * Fuegt eine Entscheidung ein und legt ihr Merkmal bei Bedarf gleich mit an
 *.
 *
 * Reihenfolge: (1) neues Datenelement anlegen, (2) Schreibbindung am Schritt
 * davor, (3) ``conditional-insert``. Jeder Schritt ist eine eigene
 * Kern-Operation mit eigener Pruefung -- keine Abkuerzung. Scheitert (2) oder
 * (3), werden die hier angelegten Teile wieder entfernt, damit kein halbes
 * Merkmal im Modell zurueckbleibt; die Fehlermeldung des Kerns wird
 * weitergereicht.
 *
 * @param {string} afterNodeId Einfuegestelle
 * @param {object[]} branches Zweige fuer ``conditional-insert``
 * @param {{existing: (string|null), newName: string, newType: string, bindAt: (string|null)}} opt
 */
async function insertConditionalWithDiscriminator(afterNodeId, branches, opt) {
  const sid = state.schemaId;
  if (!opt.existing && opt.newName && !opt.bindAt) {
    throw { detail: "Ein neues Merkmal braucht einen Schritt davor, der es setzt." };
  }
  // (1) Anlegen und dessen Aufraeumen uebernimmt ``withDiscriminator``.
  await withDiscriminator(sid, opt, async (disc) => {
    let bound = false;
    try {
      if (opt.bindAt) {
        const already = (state.schema.data_accesses || []).some((a) => a.node_id === opt.bindAt
          && a.element_id === disc && (a.mode === "WRITE" || a.mode === "READ_WRITE"));
        if (!already) {
          await createDataAccess(sid, opt.bindAt, disc, "WRITE", true);  // (2)
          bound = true;
        }
      }
      await api.post(`/schemas/${sid}/conditional-insert`, { after_node_id: afterNodeId, discriminator: disc, branches });  // (3)
    } catch (err) {
      if (bound) { try { await api.del(`/schemas/${sid}/data-access/${opt.bindAt}/${disc}?mode=WRITE`); } catch (e) { /* egal */ } }
      throw err;
    }
  });
}

/**
 * Die Schritte, denen fuer die Freigabe noch etwas fehlt (Stufe B, Regel B2).
 *
 * Kommt aus dem Kern (`GET /schemas/{id}/validation` -> `release_findings`); der
 * Client entscheidet nichts selbst, er zeigt nur an. Leeres Array = bereit.
 *
 * @returns {Array<object>} Die Stufe-B-Befunde der aktuellen Validierung.
 */
function releaseFindings() {
  const v = state.validation;
  return (v && v.release_findings) || [];
}

/**
 * Gibt das Schema frei -- sofern es freigabereif ist.
 *
 * Der Kern lehnt eine Freigabe mit Schritten ohne Bearbeiter ab (Stufe B,
 * Regel B2): ein solcher Schritt wird zur Laufzeit zwar aktiviert, taucht aber
 * in **keiner** Arbeitsliste auf (`open_tasks` ueberspringt ihn) -- der Vorgang
 * saehe gestartet aus und stuende still, und das faellt nicht dem Modellierer
 * auf, sondern spaeter dem Sachbearbeiter.
 *
 * Die Pruefung hier ist **kein zweiter Entscheider**, sondern nimmt dem Nutzer
 * den vergeblichen Weg ab: Sie nennt die betroffenen Schritte beim Namen, statt
 * ihn in ein 422 laufen zu lassen, dessen Befunde er selbst zuordnen muesste.
 * Entschieden wird ausschliesslich im Kern.
 */
async function releaseSchema() {
  const missing = releaseFindings();
  if (missing.length) {
    const names = missing.map((f) => nodeLabelOf(f.node_id)).filter(Boolean);
    // Die Meldung ist bereits eine Aufzaehlung (<ul>) -- kein eigenes „•“
    // davor, sonst standen zwei Zeichen vor jedem Namen.
    toast("err", "Freigabe noch nicht möglich", [
      missing.length === 1 ? "1 Schritt braucht noch eine Bearbeiterzuordnung:"
        : `${missing.length} Schritte brauchen noch eine Bearbeiterzuordnung:`,
      ...names,
      "Sie würden sonst in keiner Arbeitsliste erscheinen.",
    ]);
    return;
  }
  // Freigeben ist nicht umkehrbar (R0): erst nachfragen.
  openModal("Schema freigeben?", el("div", { class: "form-grid" },
    el("p", null, `„${state.schema.name}“ wird freigegeben. Danach lassen sich Vorgänge starten, das Schema selbst ist aber unveränderlich.`),
    el("p", { class: "muted" }, "Änderungen gehen dann nur noch über eine neue Revision; laufende Vorgänge lassen sich auf sie migrieren.")),
  async () => {
    try {
      await api.post(`/schemas/${state.schemaId}/release`);
      await refreshSchema();
      render();
      toast("ok", "Schema freigegeben", ["Jetzt instanziierbar."]);
    } catch (err) { toastError(err); return false; }
  }, "Freigeben");
}

/**
 * Ist das Schema laut letzter Pruefung freigabereif? Nur fuer die Farbe des
 * Knopfs -- entschieden wird beim Freigeben im Kern.
 * @param {object} schema angezeigtes Schema
 * @returns {boolean}
 */
function isReleasable(schema) {
  const v = state.validation;
  if (!v || !schema || schema.id !== state.schemaId) return false;
  return v.correct !== false && !releaseFindings().length;
}

/** Bezeichnung eines Knotens der aktuellen Sicht (fuer Meldungen). */
function nodeLabelOf(nodeId) {
  const node = nodeId && state.schema && state.schema.nodes[nodeId];
  return node ? (node.label || nodeId) : nodeId;
}

async function newRevision() {
  try {
    const rev = await api.post(`/schemas/${state.schemaId}/revision`, {});
    await loadSchemas();
    await selectSchema(rev.id);
    toast("ok", "Revision erstellt", [`${rev.name} (v${rev.version})`]);
  } catch (err) { toastError(err); }
}

// --------------------------------------------------------------------------
// Migrationsassistent (laufende Instanzen auf eine neue Revision heben)
// --------------------------------------------------------------------------

/**
 * Kopfzeilen-Knopf „Laufende Instanzen migrieren (N)“.
 *
 * Erscheint nur bei einer freigegebenen Revision, fuer die der Kern laufende
 * Instanzen frueherer Versionen meldet (``state.migrationReport``, geladen in
 * ``refreshSchema``). ``modelHeader`` ist beiden Modellier-Oberflaechen
 * gemeinsam, der Knopf existiert damit in beiden.
 *
 * @param {object} schema das angezeigte Schema
 * @param {boolean} draft ob es ein Entwurf ist
 * @returns {HTMLElement|null}
 */
function migrationHeaderButton(schema, draft) {
  const report = state.migrationReport;
  if (draft || !report || report.target_schema_id !== schema.id || !report.candidates.length) return null;
  const ok = report.candidates.filter((c) => c.migratable).length;
  return el("button", {
    class: "btn small primary", "data-tour": "model.migrate",
    title: `${countLabel(report.candidates.length, "laufende Instanz", "laufende Instanzen")} früherer Versionen, ${ok} davon sofort migrierbar`,
    onClick: () => openMigrationAssistant(schema.id, null),
  }, `Laufende Instanzen migrieren (${report.candidates.length})`);
}

/**
 * Hinweis in der Instanz-Ansicht, wenn eine neuere freigegebene Version existiert.
 * @param {object} inst die angezeigte Instanz
 * @returns {Promise<HTMLElement|null>}
 */
async function instanceMigrationPanel(inst) {
  // Die Laufzeit-Sichten zeichnen im Takt neu (Revisions-Poll); die Antwort
  // wird deshalb je Instanz+Version 30 s gemerkt, statt bei jedem Neuzeichnen
  // alle Schemata serverseitig durchzusehen.
  const key = `${inst.id}|${inst.schema_id}`;
  const cache = state.migrationTargetCache || (state.migrationTargetCache = {});
  let target = cache[key] && Date.now() - cache[key].at < 30000 ? cache[key].target : undefined;
  if (target === undefined) {
    try { target = await api.get(`/instances/${inst.id}/migration-target`); } catch (e) { return null; }
    cache[key] = { at: Date.now(), target };
  }
  if (!target || !target.schema_id) return null;
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Neue Version verfügbar"),
      el("span", { class: "sub" }, `v${inst.schema_version} → v${target.version}`)),
    el("div", { class: "panel-b row" },
      el("span", { class: "muted", style: "font-size:12px;flex:1" },
        "Diese Instanz läuft auf einer älteren Version. Ob sie wechseln kann, prüft der Kern – bereits Erledigtes bleibt unverändert."),
      el("button", { class: "btn small primary", onClick: () => openMigrationAssistant(target.schema_id, [inst.id]) },
        "Auf neue Version migrieren")));
}

/** Eingabeart eines Startwert-Felds nach Datentyp. */
function startValueWidget(elem) {
  if (!elem) return "TEXT";
  if (elem.data_type === "BOOLEAN") return "CHECKBOX";
  if (isNumericType(elem.data_type)) return "NUMBER";
  return "TEXT";
}

/**
 * Der Migrationsassistent.
 *
 * Zeigt je laufender Instanz frueherer Versionen, ob sie auf ``targetId``
 * wechseln kann, und warum nicht (Klartext aus dem Meldungskatalog). Fehlen
 * Pflichtdaten, die die neue Version verlangt und kein spaeterer Schritt mehr
 * liefert (M4), fragt er Startwerte ab -- einmal fuer alle; der Kern setzt sie
 * nur dort ein, wo die Instanz den Wert noch nicht hat. „Erneut prüfen“ ist ein
 * Trockenlauf; migriert wird erst mit dem Bestaetigen, und zwar jede Instanz
 * einzeln und atomar: eine abgelehnte bleibt unveraendert, die anderen wechseln.
 *
 * Der Client entscheidet nichts -- er zeigt nur an, was der Kern meldet.
 *
 * @param {string} targetId Schema-ID der freigegebenen Zielversion
 * @param {string[]|null} onlyIds auf diese Instanzen beschraenken (null = alle)
 */
async function openMigrationAssistant(targetId, onlyIds) {
  let report, target;
  try {
    [report, target] = await Promise.all([
      api.get(`/schemas/${targetId}/migration-report`),
      api.get(`/schemas/${targetId}`),
    ]);
  } catch (err) { toastError(err); return; }
  const cands = onlyIds ? report.candidates.filter((c) => onlyIds.includes(c.instance_id)) : report.candidates;
  if (!cands.length) {
    toast("info", "Nichts zu migrieren", ["Es laufen keine Instanzen früherer Versionen."]);
    return;
  }
  // Benennende Werte statt nackter Kennungen (best effort; ohne sie steht
  // „Vorgang“ mit der Kennung darunter).
  let migTitles = {};
  try { migTitles = await api.get("/instance-titles"); } catch (_e) { migTitles = {}; }

  // Startwerte fuer fehlende Pflichtdaten (Vereinigung ueber alle Kandidaten).
  const inputs = {};
  const missing = [...new Set(cands.flatMap((c) => c.missing_data))];
  const valueBox = el("div", { class: "form-grid" });
  missing.forEach((eid) => {
    const elem = target.data_elements[eid];
    const { control, read } = maskControl(elem, startValueWidget(elem), null, undefined);
    inputs[eid] = { read, elem };
    valueBox.appendChild(el("label", { class: "field" }, `${elem ? elem.name : eid} (${elem ? typeName(elem.data_type) : "?"})`, control));
  });
  const readMapping = () => {
    const mapping = {};
    for (const [eid, { read }] of Object.entries(inputs)) {
      const v = read();
      if (v === undefined || (typeof v === "number" && Number.isNaN(v))) continue;
      mapping[eid] = v;
    }
    return mapping;
  };

  // Tabelle: Auswahl, Instanz, Version, Ergebnis der Pruefung.
  const checks = {};
  const statusCells = {};
  const rowsById = {};
  const migRows = cands.map((c) => {
    const box = el("input", { type: "checkbox" });
    box.checked = c.migratable;
    checks[c.instance_id] = box;
    statusCells[c.instance_id] = el("div", { class: "mig-status" });
    return [box, instanceNameCell(c.instance_id, c.started_at, migTitles[c.instance_id]),
      `v${c.schema_version}`, statusCells[c.instance_id]];
  });
  const migTable = table(["Auswahl", "Instanz", "Version", "Ergebnis"], migRows, null, { class: "mig-table" });
  migTable.querySelectorAll("tbody tr").forEach((tr, i) => { rowsById[cands[i].instance_id] = tr; });
  const showStatus = (iid, findings) => {
    const cell = statusCells[iid];
    clear(cell);
    if (!findings.length) { cell.appendChild(el("span", { class: "pill pill-green" }, "migrierbar")); return; }
    cell.appendChild(el("span", { class: "pill pill-amber" }, "bleibt auf ihrer Version"));
    findings.forEach((f) => cell.appendChild(el("div", { class: "mig-reason" }, findingText(f, { withHint: true }))));
  };
  cands.forEach((c) => showStatus(c.instance_id, c.findings));

  const recheck = el("button", { class: "btn small", onClick: async () => {
    try {
      const res = await api.post(`/schemas/${targetId}/migrate-instances`,
        { instance_ids: cands.map((c) => c.instance_id), data_mapping: readMapping(), execute: false });
      res.results.forEach((r) => { showStatus(r.instance_id, r.findings); checks[r.instance_id].checked = !r.findings.length; });
    } catch (err) { toastError(err); }
  } }, "Mit diesen Startwerten erneut prüfen");

  const body = el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      `Ziel: ${target.name} v${target.version}. Bereits Erledigtes bleibt, wie es ist; eine Instanz, die nicht passt, läuft sicher auf ihrer Version weiter.`),
    missing.length
      ? el("div", { class: "mig-values" },
          el("div", { style: "font-weight:600;font-size:13px;margin-bottom:6px" }, "Startwerte für neue Pflichtdaten"),
          valueBox, recheck)
      : null,
    migTable);

  openModal(`Instanzen auf v${target.version} migrieren`, body, async () => {
    const ids = cands.map((c) => c.instance_id).filter((iid) => checks[iid].checked);
    if (!ids.length) { toast("info", "Keine Instanz ausgewählt"); return false; }
    try {
      const res = await api.post(`/schemas/${targetId}/migrate-instances`,
        { instance_ids: ids, data_mapping: readMapping(), execute: true });
      const moved = res.results.filter((r) => r.migrated);
      const kept = res.results.filter((r) => !r.migrated);
      kept.forEach((r) => showStatus(r.instance_id, r.findings));
      // Bereits migrierte verschwinden aus dem offenen Dialog -- ein zweiter
      // Durchgang soll nur noch die uebrigen betreffen.
      moved.forEach((r) => {
        rowsById[r.instance_id].remove();
        const i = cands.findIndex((c) => c.instance_id === r.instance_id);
        if (i >= 0) cands.splice(i, 1);
      });
      state.migrationTargetCache = {};
      toast(kept.length ? "err" : "ok", `${countLabel(moved.length, "Instanz", "Instanzen")} migriert` + (kept.length ? `, ${kept.length} nicht` : ""),
        kept.map((r) => `${r.instance_id}: ${r.findings.map((f) => findingText(f)).join(" ")}`));
      if (state.instance && moved.some((r) => r.instance_id === state.instance.id)) await loadInstance(state.instance.id);
      if (state.schemaId) await refreshSchema();
      render();
      return kept.length ? false : undefined;   // bei Rest offen lassen, damit die Gruende lesbar bleiben
    } catch (err) { toastError(err); return false; }
  }, "Ausgewählte migrieren");
}

async function exportBpmn() {
  try {
    const xml = await api.raw(`/schemas/${state.schemaId}/bpmn`);
    const blob = new Blob([xml], { type: "application/xml" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `${state.schema.name}.bpmn` });
    document.body.appendChild(a); a.click(); a.remove();
  } catch (err) { toastError(err); }
}

// --------------------------------------------------------------------------
// View: Datensicht
// --------------------------------------------------------------------------

/**
 * Nimmt ein Datenelement in die benennenden Werte des Vorgangs auf oder
 * entfernt es. Geschrieben wird ueber den Kern
 * (``POST /schemas/{id}/display-fields``), der U5 prueft -- beim dritten
 * Element kommt dessen Meldung.
 * @param {string} elementId Datenelement
 */
async function toggleDisplayField(elementId) {
  const current = (state.schema.display_fields || []).slice();
  const next = current.includes(elementId)
    ? current.filter((e) => e !== elementId)
    : [...current, elementId];
  try {
    await api.post(`/schemas/${state.schemaId}/display-fields`, { element_ids: next });
    await refreshSchema(); render();
  } catch (err) { toastError(err); }
}

function viewData() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;
  const draft = isDraft(schema);

  // Datenelemente
  const elemRows = Object.values(schema.data_elements);
  const elemHeaders = draft ? ["Name", "Typ", "Quelle", "Aktionen"] : ["Name", "Typ", "Quelle"];
  const elemTable = elemRows.length
    ? table(elemHeaders, elemRows.map((d) => {
        const cells = [d.name, typeName(d.data_type), DATA_SOURCE_LABELS[d.source] || d.source];
        if (draft) {
          // Benennt den Vorgang in Listen; hoechstens zwei, nur
          // Vorgangsdaten -- das prueft der Kern (U5).
          const shown = (schema.display_fields || []).includes(d.id);
          const nameBtn = d.source === "INSTANCE"
            ? el("button", { class: "btn small" + (shown ? " primary" : " ghost"),
                title: "Der Wert benennt Vorgang und Aufgaben in Listen (höchstens zwei Datenelemente)",
                onClick: () => toggleDisplayField(d.id) }, shown ? "\u2713 benennt Vorgang" : "Benennt Vorgang")
            : null;
          const editBtn = el("button", { class: "btn small", onClick: () => editDataElement(d) }, "Bearbeiten");
          const resetBtn = d.source === "EXTERNAL"
            ? el("button", { class: "btn small", onClick: () => resetDataElementSource(d) }, "Quelle zur\u00FCcksetzen")
            : null;
          const delBtn = el("button", { class: "btn small danger", onClick: () => deleteDataElement(d) }, "L\u00F6schen");
          cells.push(el("div", { class: "row-actions" }, nameBtn, editBtn, resetBtn, delBtn));
        }
        return cells;
      }))
    : emptyState("Noch keine Datenelemente.");

  const addElemBtn = el("button", { class: "btn small", onClick: addDataElement, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Datenelement");
  const elemPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Datenelemente"), el("span", { class: "spacer", style: "flex:1" }), addElemBtn),
    el("div", { class: "panel-b" }, elemTable));

  // Datenzugriffe
  const accList = schema.data_accesses || [];
  const accRows = accList.map((a) => {
    const node = schema.nodes[a.node_id];
    const elem = schema.data_elements[a.element_id];
    return [node ? nodeCaption(node) : a.node_id, elem ? elem.name : a.element_id, accessModeLabel(a.mode), a.mandatory ? "Pflicht" : "optional"];
  });
  const accTable = accRows.length
    ? table(["Schritt", "Element", "Modus", "Bindung"], accRows,
        (i) => accList[i].node_id === state.dataFocusNode ? "hl-row" : "")
    : emptyState("Noch keine Datenbindungen.");
  const addAccBtn = el("button", { class: "btn small", onClick: addDataAccess, ...lockedBy([!draft, DRAFT_ONLY_REASON],
    [activitiesOf(schema).length === 0, "Erst einen Schritt modellieren."],
    [elemRows.length === 0, "Erst ein Datenelement anlegen."]) }, "+ Datenbindung");
  const accPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Lese-/Schreibbindungen"), el("span", { class: "sub" }, "D1-D4 live gepr\u00FCft"), el("span", { class: "spacer", style: "flex:1" }), addAccBtn),
    el("div", { class: "panel-b" }, accTable));

  const dataFocus = state.dataFocusNode && schema.nodes[state.dataFocusNode];
  const rightCol = el("div", null,
    returnBar(),
    dataFocus
      ? focusBanner("Hervorgehoben: Bindungen von \u201E" + nodeCaption(dataFocus) + "\u201C",
          () => { state.dataFocusNode = null; render(); })
      : null,
    accPanel, dFindingsPanel());

  content.appendChild(el("div", { class: "grid-2" }, elemPanel, rightCol));
  scrollHighlightIntoView();
}

function dFindingsPanel() {
  const v = state.validation;
  const dz = v && !v.correct ? v.findings.filter((f) => f.rule[0] === "D" || f.rule[0] === "C") : [];
  const body = el("div", { class: "panel-b" });
  if (!dz.length) body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Datenfluss konsistent (D/C)."));
  else dz.forEach((f) => body.appendChild(el("div", { class: "finding" }, el("span", { class: "rule" }, f.rule), el("span", null, findingText(f, { withHint: true })))));
  return el("div", { class: "panel" }, el("div", { class: "panel-h" }, el("h2", null, "Datenfluss-Befunde")), body);
}

/**
 * Legt ein Datenelement an.
 *
 * @param {function(): void} [onCreated] Optionaler Rueckweg nach dem Anlegen --
 *        der Bindungsdialog der Schritt-Karte oeffnet sich damit erneut, jetzt
 *        mit dem frisch angelegten Element in der Liste, statt den Nutzer nach
 *        dem Anlegen ohne Bindung stehen zu lassen.
 */
function addDataElement(onCreated) {
  const name = el("input", { type: "text", placeholder: "z. B. betrag" });
  const type = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t }, typeName(t))));
  openModal("Datenelement", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Typ", type)), async () => {
    if (!name.value.trim()) return false;
    try {
      const known = new Set(Object.keys(state.schema.data_elements || {}));
      const schema = await api.post(`/schemas/${state.schemaId}/data-elements`, { name: name.value.trim(), data_type: type.value });
      const created = Object.keys(schema.data_elements || {}).find((id) => !known.has(id));
      await refreshSchema(); render(); toast("ok", "Datenelement angelegt");
      // Erst NACH dem Schliessen dieses Dialogs fortsetzen: ``openModal``
      // leert nach dem Bestaetigen seinen Container -- ein hier sofort
      // geoeffneter Folgedialog verschwand mit.
      if (typeof onCreated === "function") setTimeout(() => onCreated(created), 0);
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * „+ Datenbindung“ der Datensicht: erst den Schritt waehlen, dann derselbe
 * Bindungsdialog wie an der Schritt-Karte (``bindDataDialog``: Element,
 * Richtung, Pflicht/optional). Frueher eine eigene, schwaechere Fassung, die
 * immer als Pflicht band -- die D1-Abhilfe „nicht als Pflicht setzen“ war dort
 * nicht moeglich.
 *
 * @param {string} [fixedNodeId] Schritt vorgeben (dann direkt der Bindungsdialog)
 */
function addDataAccess(fixedNodeId) {
  const schema = state.schema;
  if (typeof fixedNodeId === "string") { bindDataDialog(fixedNodeId); return; }
  const nodeSel = el("select", null, ...activitiesOf(schema).map((n) => el("option", { value: n.id }, nodeCaption(n))));
  openModal("Datenbindung – Schritt wählen", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Schritt", nodeSel)), async () => {
    // Der Bindungsdialog ersetzt diesen; false, sonst schloesse openModal ihn.
    bindDataDialog(nodeSel.value);
    return false;
  }, "Weiter");
}

function editDataElement(elem) {
  const name = el("input", { type: "text", value: elem.name });
  const type = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t, selected: t === elem.data_type ? "selected" : null }, typeName(t))));
  openModal("Datenelement bearbeiten", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Typ", type)), async () => {
    if (!name.value.trim()) return false;
    return commitSchemaChange(() => api.patch(`/schemas/${state.schemaId}/data-elements/${elem.id}`, { name: name.value.trim(), data_type: type.value }), "Datenelement aktualisiert");
  }, "Speichern");
}

function resetDataElementSource(elem) {
  openModal("Quelle zur\u00FCcksetzen",
    el("p", null, `Externe Bindung von \u201E${elem.name}\u201C entfernen und wieder als Instanz-Datenelement f\u00FChren?`),
    async () => {
      return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${elem.id}/reset-source`, {}), "Quelle zur\u00FCckgesetzt");
    }, "Zur\u00FCcksetzen", { danger: true });
}

function deleteDataElement(elem) {
  openModal("Datenelement l\u00F6schen",
    el("p", null, `Datenelement \u201E${elem.name}\u201C und alle zugeh\u00F6rigen Lese-/Schreibbindungen und Maskenfelder l\u00F6schen?`),
    async () => {
      return commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/data-elements/${elem.id}`), "Datenelement gel\u00F6scht");
    }, "L\u00F6schen", { danger: true });
}

// --------------------------------------------------------------------------
// View: Ressourcensicht
// --------------------------------------------------------------------------

function viewOrg() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;
  const draft = isDraft(schema);
  const linked = !!schema.org_model_id;
  // A shared organisation is editable independently of this schema's lifecycle
  // (it is master data used across models); a local org follows the draft gate.
  const orgEditable = draft || linked;
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };

  const orgPanel = sharedOrgBanner(schema, draft);

  const rolePanel = rolesPanel(org, orgEditable);

  const unitPanel = orgUnitPanel(org, orgEditable);
  const licPanel = licensePanel();
  const agentPanel = agentListPanel(org, orgEditable);
  // Fire-and-forget refresh of the licensing snapshot; re-renders only when the
  // data actually changed (guarded by a signature), so there is no render loop.
  refreshLicenseInfo();

  // BZR-Zuordnung
  const ruleEntries = Object.entries(schema.staff_rules || {});
  const ruleRows = ruleEntries.map(([nid, rule]) => {
    const node = schema.nodes[nid];
    return [node ? nodeCaption(node) : nid, describeRule(rule)];
  });
  const ruleTable = ruleRows.length
    ? table(["Schritt", "Bearbeiterregel"], ruleRows,
        (i) => ruleEntries[i][0] === state.staffFocusNode ? "hl-row" : "")
    : emptyState("Noch keine Bearbeiterzuordnung.");
  const addRuleBtn = el("button", { class: "btn small", onClick: addStaffRule,
    ...lockedBy([!draft, DRAFT_ONLY_REASON],
      [activitiesOf(schema).length === 0, "Erst einen Schritt modellieren."],
      [(Object.keys(org.roles || {}).length + Object.keys(org.org_units || {}).length) === 0,
        "Erst eine Rolle oder Abteilung anlegen."]) }, "+ Zuordnung");
  const rulePanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Bearbeiterzuordnung (BZR)"), el("span", { class: "sub" }, "Z1-Z4 live"), el("span", { class: "spacer", style: "flex:1" }), addRuleBtn),
    el("div", { class: "panel-b" }, ruleTable));

  const staffFocus = state.staffFocusNode && schema.nodes[state.staffFocusNode];
  const orgFocusUnit = state.orgFocusUnit && (org.org_units || {})[state.orgFocusUnit];
  content.appendChild(el("div", { class: "grid-2" },
    el("div", null, orgPanel, licPanel, rolePanel, unitPanel, agentPanel),
    el("div", null,
      returnBar(),
      staffFocus
        ? focusBanner("Hervorgehoben: Zuordnung von \u201E" + nodeCaption(staffFocus) + "\u201C",
            () => { state.staffFocusNode = null; render(); })
        : null,
      rulePanel, zFindingsPanel(),
      orgFocusUnit
        ? focusBanner("Hervorgehoben: Abteilung \u201E" + orgFocusUnit.name + "\u201C inkl. zugeh\u00F6riger Agenten",
            () => { state.orgFocusUnit = null; state.orgFocusAgents = []; render(); })
        : null,
      orgChartPanel(org))));
  scrollHighlightIntoView();
}

// --------------------------------------------------------------------------
// Licensing / agent metering (agent page). All of this stays hidden while the
// backend runs in the default "open" mode (enforced=false): the panel and the
// per-agent badges only render once a licensor key is configured, so the
// dormant state is visually indistinguishable from before licensing existed.
// --------------------------------------------------------------------------

// Refresh the licensing snapshot in the background. Re-renders the org view
// only when the fetched data changed (signature compare) to avoid a loop; on
// any error (e.g. old backend without the endpoints) it silently gives up.
function refreshLicenseInfo() {
  Promise.all([api.get("/license/status"), api.get("/license/agents")])
    .then(([status, agents]) => {
      const sig = JSON.stringify([status, agents]);
      if (sig === state._licenseSig) return;
      state._licenseSig = sig;
      state.license = status;
      state.licenseAgents = {};
      (agents || []).forEach((a) => { state.licenseAgents[a.agent_id] = a; });
      if (state.view === "org") render();
    })
    .catch(() => {});
}

// The contingent panel: a used/total quota bar, a staged expiry banner and the
// "+5 Agenten kaufen" call-to-action. Returns null (no panel) unless licensing
// is actively enforced.
function licensePanel() {
  const lic = state.license;
  if (!lic || !lic.enforced) return null;
  const total = lic.total_slots || 0;
  const used = lic.used_slots || 0;
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const buyBtn = el("button", { class: "btn small primary", onClick: buyAgentPack }, "+5 Agenten kaufen");
  const head = el("div", { class: "panel-h" }, el("h2", null, "Lizenz"),
    el("span", { class: "sub" }, `${used}/${total} Agenten belegt`),
    el("span", { class: "spacer", style: "flex:1" }), buyBtn);
  const bar = el("div", { class: "lic-bar", title: `${used} von ${total} lizenzierten Agenten-Slots belegt` },
    el("div", { class: "lic-bar-fill", style: `width:${pct}%` }));
  const body = el("div", { class: "panel-b" }, bar);
  // Staged renewal reminder (30/14/7/1 days), computed locally from the ratchet.
  const days = lic.days_to_next_expiry;
  if (days != null && days <= 30) {
    const when = new Date((lic.next_expiry_at || 0) * 1000).toLocaleDateString("de-DE");
    body.appendChild(el("div", { class: "lic-banner" + (days <= 7 ? " urgent" : "") },
      `Ein Agenten-Paket läuft in ${days} Tag${days === 1 ? "" : "en"} ab (${when}) – jetzt verlängern.`));
  }
  if (lic.packs_active === 0 && used >= total) {
    body.appendChild(el("div", { class: "sub", style: "margin-top:8px" },
      "Free-Kontingent ausgeschöpft. Mit einem Paket weitere Agenten freischalten."));
  }
  return el("div", { class: "panel" }, head, body);
}

// Per-agent licensing badge for the Agenten table (only when enforced).
function agentLicenseBadge(agentId) {
  const v = (state.licenseAgents || {})[agentId];
  if (!v) return el("span", { class: "muted" }, "–");
  if (v.licensed) {
    const label = v.days_left != null ? `${v.days_left} Tage` : "gültig";
    return el("span", { class: "pill pill-green", title: "Lizenziert" }, label);
  }
  return el("span", { class: "pill pill-red", title: "Nicht lizenziert – Paket kaufen/verlängern" }, "ungedeckt");
}

// Start a purchase. The checkout itself runs on a separate licensor backend;
// with none configured the API reports so and we show a friendly note.
async function buyAgentPack() {
  try {
    const res = await api.post("/license/checkout", { slots: 5, months: 12 });
    if (res.checkout_url) {
      window.open(res.checkout_url, "_blank", "noopener");
      if (res.claim_token) {
        // Online auto-pull configured: keep polling the licensor in the
        // background and activate the pack automatically once it is issued,
        // so the operator does not have to copy-&-paste the token.
        toast("ok", "Kauf gestartet", ["Bitte im neuen Tab abschließen – die Lizenz wird danach automatisch eingespielt."]);
        pollClaimsUntilActivated();
      } else {
        toast("ok", "Kauf gestartet", ["Bitte im neuen Tab abschließen; die Lizenz danach unter „Aktivieren“ einspielen."]);
      }
    } else {
      toast("err", "Kauf nicht verfügbar", [res.message]);
    }
  } catch (err) {
    toastError(err);
  }
}

// Best-effort online auto-pull: after a checkout with a claim token, poll the
// backend (which in turn contacts the separate licensor) every few seconds
// until a pack is activated or no claim is left pending. Never blocks anything;
// the manual "Aktivieren" copy-&-paste path always remains available as a
// fallback. Runs at most a bounded number of passes so it cannot loop forever.
async function pollClaimsUntilActivated() {
  if (state._claimPolling) return;
  state._claimPolling = true;
  const INTERVAL_MS = 6000;
  const MAX_PASSES = 60; // ~6 min ceiling; matches the short claim TTL
  try {
    for (let i = 0; i < MAX_PASSES; i++) {
      await new Promise((r) => setTimeout(r, INTERVAL_MS));
      let res;
      try {
        res = await api.post("/license/claims/poll", {});
      } catch (_e) {
        continue; // transient; try again next pass
      }
      if (res && res.activated > 0) {
        state._licenseSig = null; // force a re-render on the next refresh
        refreshLicenseInfo();
        toast("ok", "Lizenz aktiviert", ["Das gekaufte Agenten-Paket wurde automatisch eingespielt."]);
        return;
      }
      if (!res || res.pending === 0) return; // nothing left to wait for
    }
  } finally {
    state._claimPolling = false;
  }
}

// Endpoint base for org-entity edits: the shared org registry when the schema
// is linked, otherwise the schema's embedded org. The same path suffixes
// (/roles, /org-units, /agents, ...) exist under both bases.
function orgApi(suffix) {
  const oid = state.schema && state.schema.org_model_id;
  return oid ? `/org-models/${oid}${suffix}` : `/schemas/${state.schemaId}${suffix}`;
}

function sharedOrgBanner(schema, draft) {
  const linked = !!schema.org_model_id;
  const head = el("div", { class: "panel-h" }, el("h2", null, "Organisation"),
    el("span", { class: "sub" }, linked ? "geteilt (modell\u00FCbergreifend)" : "lokal in diesem Modell"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: manageSharedOrg }, linked ? "Verwalten" : "Geteilte Organisation\u2026"));
  const body = el("div", { class: "panel-b" }, el("div", { class: "sub" }, linked
    ? "Diese Organisation wird zentral gepflegt; \u00C4nderungen wirken sofort in allen verkn\u00FCpften Modellen."
    : "Die Organisation geh\u00F6rt nur zu diesem Modell. Verkn\u00FCpfe sie, um dieselbe Organisation in mehreren Modellen zu verwenden."));
  return el("div", { class: "panel" }, head, body);
}

async function manageSharedOrg() {
  const schema = state.schema;
  const draft = isDraft(schema);
  let orgs = [];
  try { orgs = await api.get("/org-models"); } catch (err) { orgs = []; }

  if (schema.org_model_id) {
    const cur = orgs.find((o) => o.id === schema.org_model_id);
    const body = el("div", null,
      el("div", { class: "field" }, "Verkn\u00FCpft mit geteilter Organisation: ",
        el("strong", null, cur ? cur.name : schema.org_model_id)),
      el("p", { class: "sub", style: "margin-top:10px" }, draft
        ? "Beim L\u00F6sen wird die aktuelle Organisation als lokale Kopie ins Modell \u00FCbernommen."
        : "Zum L\u00F6sen der Verkn\u00FCpfung muss das Schema im Entwurf sein."));
    openModal("Geteilte Organisation", body, draft ? async () => {
      return commitSchemaChange(() => api.del(`/schemas/${state.schemaId}/org-model`), "Verkn\u00FCpfung gel\u00F6st");
    } : null, draft ? "Verkn\u00FCpfung l\u00F6sen" : "Schliessen");
    return;
  }

  const sel = el("select", null, el("option", { value: "" }, "\u2013 vorhandene w\u00E4hlen \u2013"),
    ...orgs.map((o) => el("option", { value: o.id }, o.name)));
  const newName = el("input", { type: "text", placeholder: "z. B. Stadtverwaltung" });
  const body = el("div", null,
    el("label", { class: "field" }, "Vorhandene Organisation", sel),
    el("div", { class: "sub", style: "margin:10px 0" }, "\u2013 oder neue anlegen \u2013"),
    el("label", { class: "field" }, "Name", newName),
    draft ? null : el("p", { class: "sub", style: "margin-top:10px" }, "Verkn\u00FCpfen ist nur im Entwurf m\u00F6glich."));
  openModal("Geteilte Organisation verkn\u00FCpfen", body, async () => {
    if (!draft) { toast("err", "Nur im Entwurf m\u00F6glich"); return false; }
    try {
      let orgId = sel.value;
      if (!orgId) {
        if (!newName.value.trim()) return false;
        const created = await api.post("/org-models", { name: newName.value.trim() });
        orgId = created.id;
      }
      await api.post(`/schemas/${state.schemaId}/org-model`, { org_model_id: orgId });
      await refreshSchema(); render(); toast("ok", "Mit geteilter Organisation verkn\u00FCpft");
    } catch (err) { toastError(err); return false; }
  }, "Verkn\u00FCpfen");
}

function orgUnitPanel(org, draft) {
  const units = Object.values(org.org_units || {});
  const head = el("div", { class: "panel-h" }, el("h2", null, "Abteilungen"),
    el("span", { class: "sub" }, "Hierarchie mit Vorgesetzten"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: () => addChildOrgUnit(null), ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Abteilung"));
  const body = el("div", { class: "panel-b" });
  if (!units.length) { body.appendChild(emptyState("Noch keine Abteilung.")); return el("div", { class: "panel" }, head, body); }

  // Kinder je Elternknoten indexieren; verwaiste (unbekannter Parent) auf oberste Ebene.
  const known = org.org_units || {};
  const childrenOf = {};
  units.forEach((u) => {
    const p = u.parent_id && known[u.parent_id] ? u.parent_id : "__root__";
    (childrenOf[p] = childrenOf[p] || []).push(u);
  });
  Object.values(childrenOf).forEach((list) => list.sort((a, b) => a.name.localeCompare(b.name)));

  const tree = el("div", { class: "tree" });
  (childrenOf["__root__"] || []).forEach((u) => tree.appendChild(renderUnitNode(u, org, draft, childrenOf)));
  body.appendChild(tree);
  return el("div", { class: "panel" }, head, body);
}

function renderUnitNode(unit, org, draft, childrenOf) {
  const mgr = unit.manager_id && org.agents[unit.manager_id] ? org.agents[unit.manager_id].name : null;
  const rowCls = "tree-row" + (unit.id === state.orgFocusUnit ? " tree-row-hl" : "");
  const row = el("div", { class: rowCls },
    el("span", { class: "tree-name" }, unit.name),
    mgr
      ? el("span", { class: "tree-badge tree-badge-link", title: "Vorgesetzten in der Agentenliste hervorheben",
          onClick: () => focusOrgAgent(unit.manager_id, unit.id) }, "\u2605 " + mgr)
      : el("span", { class: "tree-badge muted-badge" }, "kein Vorgesetzter"),
    unit.mailbox
      ? el("span", { class: "tree-badge", title: "Abteilungspostfach (Gruppen-Benachrichtigung)" }, "\u2709 " + unit.mailbox)
      : null,
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: () => editManager(unit) }, "Vorgesetzter"),
    el("button", { class: "btn small", onClick: () => editUnitMailbox(unit) }, "Postfach"),
    el("button", { class: "btn small", onClick: () => moveOrgUnit(unit) }, "Umh\u00E4ngen"),
    draft ? el("button", { class: "btn small", onClick: () => addChildOrgUnit(unit.id) }, "+ Unter") : null);
  const node = el("div", { class: "tree-node" }, row);
  const kids = childrenOf[unit.id] || [];
  if (kids.length) {
    const childWrap = el("div", { class: "tree-children" });
    kids.forEach((c) => childWrap.appendChild(renderUnitNode(c, org, draft, childrenOf)));
    node.appendChild(childWrap);
  }
  return node;
}

function agentListPanel(org, draft) {
  const agents = Object.values(org.agents || {});
  // Login-Spalte (nur Admin, Passwort-Modus): wird nach dem Laden der Logins
  // gefuellt (fillLoginSlots), damit die Tabelle nicht auf /users wartet.
  const loginSlots = {};
  // The licensing badge column only appears while enforcement is active.
  const showLicense = !!(state.license && state.license.enforced);
  const head = el("div", { class: "panel-h" }, el("h2", null, "Agenten"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", onClick: addAgent, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Agent"));
  const body = el("div", { class: "panel-b" });
  if (!agents.length) body.appendChild(emptyState("Noch keine Agenten."));
  else {
    const rows = agents.map((a) => {
      const roles = (a.role_ids || []).map((r) => org.roles[r] ? org.roles[r].name : r).join(", ") || "\u2013";
      const unit = a.org_unit_id && org.org_units[a.org_unit_id] ? org.org_units[a.org_unit_id].name : "\u2013";
      const dep = a.deputy_id && org.agents[a.deputy_id] ? org.agents[a.deputy_id].name : "\u2013";
      const editBtn = el("button", { class: "btn small", onClick: () => editAgent(a), ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "Bearbeiten");
      const depBtn = el("button", { class: "btn small", onClick: () => editDeputy(a) }, "Vertreter");
      const actions = el("div", { style: "display:flex; gap:6px; justify-content:flex-end" }, editBtn, depBtn);
      // Login provisioning is an admin-only convenience available in password
      // mode; it is independent of the schema lifecycle (works on shared orgs).
      if (state.passwordLogin && hasRole("admin")) {
        const slot = el("span", { class: "login-slot" });
        loginSlots[a.id] = { slot, agent: a };
        actions.appendChild(slot);
      }
      const email = a.email
        ? a.email
        : el("span", { class: "muted" }, "–");
      const row = [a.name, email, roles, unit, dep];
      if (showLicense) row.push(agentLicenseBadge(a.id));
      row.push(actions);
      return row;
    });
    const cols = ["Agent", "E-Mail", "Rollen", "Abteilung", "Vertreter"];
    if (showLicense) cols.push("Lizenz");
    cols.push("");
    body.appendChild(table(cols, rows,
      (i) => (state.orgFocusAgents || []).includes(agents[i].id) ? "hl-row" : ""));
    if (Object.keys(loginSlots).length) fillLoginSlots(loginSlots);
  }
  return el("div", { class: "panel" }, head, body);
}

/**
 * Fuellt die Login-Spalte der Agententabelle: bestehender Login oder Knopf.
 *
 * Hat eine Person schon einen Login (``GET /users``, Feld ``agent_id``),
 * steht dort „Login: <name>“ mit Verweis auf Administration → Benutzer –
 * ein zweiter Klick legte sonst still einen weiteren Login (``name2``) an.
 * Sonst der Knopf „Login“ (``provisionLogin``). Scheitert das Laden, bleibt
 * es beim Knopf: Anlegen ist dann weiterhin moeglich, der Kern lehnt einen
 * doppelten Loginnamen ohnehin ab.
 *
 * @param {Object<string, {slot: HTMLElement, agent: object}>} slots je Agent-id
 * @returns {Promise<void>}
 */
async function fillLoginSlots(slots) {
  let users = [];
  try { users = await api.get("/users"); } catch (_e) { users = []; }
  const byAgent = {};
  (users || []).forEach((u) => { if (u.agent_id && !byAgent[u.agent_id]) byAgent[u.agent_id] = u; });
  Object.entries(slots).forEach(([agentId, { slot, agent }]) => {
    clear(slot);
    const u = byAgent[agentId];
    slot.appendChild(u
      ? el("span", { class: "muted login-exists",
          title: "Passwort zur\u00FCcksetzen oder l\u00F6schen unter Administration \u2192 Benutzer" },
          `Login: ${u.login}`)
      : el("button", { class: "btn small", onClick: () => provisionLogin(agent) }, "Login"));
  });
}

// Organigramm: the org-unit hierarchy rendered as a classic top-down org chart
// (HTML/CSS, no SVG). Clicking a box highlights that unit (in the chart and the
// Abteilungen tree) and every agent that belongs to it -- including the unit's
// supervisor -- in the Agenten table.
function orgChartPanel(org) {
  const units = Object.values(org.org_units || {});
  const head = el("div", { class: "panel-h" }, el("h2", null, "Organigramm"),
    el("span", { class: "sub" }, "Klick hebt Abteilung + Agenten hervor"));
  const body = el("div", { class: "panel-b" });
  if (!units.length) {
    body.appendChild(emptyState("Noch keine Abteilung modelliert."));
    return el("div", { class: "panel" }, head, body);
  }
  // Kinder je Elternknoten indexieren; verwaiste (unbekannter Parent) als Wurzel.
  const known = org.org_units || {};
  const childrenOf = {};
  units.forEach((u) => {
    const p = u.parent_id && known[u.parent_id] ? u.parent_id : "__root__";
    (childrenOf[p] = childrenOf[p] || []).push(u);
  });
  Object.values(childrenOf).forEach((list) => list.sort((a, b) => a.name.localeCompare(b.name)));
  const roots = childrenOf["__root__"] || [];
  body.appendChild(el("div", { class: "orgchart" },
    el("ul", null, ...roots.map((u) => orgChartNode(u, org, childrenOf)))));
  return el("div", { class: "panel" }, head, body);
}

function orgChartNode(unit, org, childrenOf) {
  const mgr = unit.manager_id && (org.agents || {})[unit.manager_id] ? org.agents[unit.manager_id].name : null;
  const memberCount = Object.values(org.agents || {}).filter((a) => a.org_unit_id === unit.id).length;
  const cls = "oc-node" + (unit.id === state.orgFocusUnit ? " selected" : "");
  const box = el("div", { class: cls, title: "Abteilung + zugeh\u00F6rige Agenten hervorheben",
    onClick: () => focusOrgUnit(unit.id) },
    el("div", { class: "oc-name" }, unit.name),
    mgr ? el("div", { class: "oc-mgr" }, "\u2605 " + mgr) : el("div", { class: "oc-mgr oc-muted" }, "kein Vorgesetzter"),
    el("div", { class: "oc-count" }, memberCount + (memberCount === 1 ? " Agent" : " Agenten")));
  const li = el("li", null, box);
  const kids = childrenOf[unit.id] || [];
  if (kids.length) li.appendChild(el("ul", null, ...kids.map((c) => orgChartNode(c, org, childrenOf))));
  return li;
}

/**
 * Bearbeiterregel in einem Satz -- mit Namen statt interner IDs.
 *
 * Ohne ``schema`` wird das gerade gewaehlte genommen; das ist in allen
 * Modellier-Sichten das richtige. Ist eine Referenz dort unbekannt (etwa in
 * einer fremden Organisation), bleibt die ID stehen, statt den Satz zu
 * verschweigen. Reine Anzeige -- keine Zustaendigkeitsentscheidung.
 *
 * @param {object} rule Bearbeiterregel (StaffRule) oder null
 * @param {object} [schema] Schema, gegen das Namen aufgeloest werden
 * @returns {string}
 */
function describeRule(rule, schema) {
  if (!rule) return "\u2013";
  const s = schema === undefined ? state.schema : schema;
  const org = (s && s.org_model) || {};
  const named = (map, id) => ((map || {})[id] || {}).name || id;
  const stepName = (id) => {
    const n = s && s.nodes ? s.nodes[id] : null;
    return n && n.label ? n.label : id;
  };
  if (rule.kind === "ROLE") return `Rolle: ${named(org.roles, rule.ref)}`;
  // „Abteilung“ wie in den Dialogen, nicht „OrgEinheit“.
  if (rule.kind === "ORG_UNIT") return `Abteilung: ${named(org.org_units, rule.ref)}${rule.recursive ? " (inkl. Unterbereiche)" : ""}`;
  // Beim Agenten lohnt der Rueckfall auf agentNameOf: kennt ihn die
  // Organisation dieses Schemas nicht, findet ihn oft das modelluebergreifende
  // Verzeichnis.
  if (rule.kind === "AGENT") {
    return `Agent: ${((org.agents || {})[rule.ref] || {}).name || agentNameOf(rule.ref)}`;
  }
  if (rule.kind === "NODE_PERFORMING_AGENT") return `Bearbeiter von \u201e${stepName(rule.ref)}\u201c`;
  if (rule.kind === "NODE_PERFORMING_AGENT_SUPERVISOR") return `Vorgesetzte:r des Bearbeiters von \u201e${stepName(rule.ref)}\u201c`;
  // Verknuepfungen im Wortlaut des Dialogs statt „AND(…, …)“.
  if (rule.operands) {
    const parts = rule.operands.map((o) => (o.operands ? `(${describeRule(o, s)})` : describeRule(o, s)));
    const word = { AND: " und ", OR: " oder ", EXCEPT: " au\u00DFer " }[rule.kind];
    return word ? parts.join(word) : `${rule.kind}(${parts.join(", ")})`;
  }
  return rule.kind;
}

/**
 * Bezieht sich eine Bearbeiterregel auf die Person, die ``nodeId`` erledigt hat?
 *
 * Solche relativen Regeln (Vier-Augen-Prinzip: "Vorgesetzte:r des Bearbeiters
 * von X") finden niemanden, wenn X per Aufsichtseingriff ohne Bearbeiter
 * abgeschlossen wurde -- dann steht kein Ausfuehrer im Vorgang, auf den sie sich
 * beziehen koennten. Rein lesend; der Client entscheidet daraus nichts, er
 * erklaert es nur.
 *
 * @param {object} rule Bearbeiterregel
 * @param {string} nodeId Knoten, auf den sich die Regel beziehen koennte
 * @returns {boolean}
 */
function ruleRefersToPerformerOf(rule, nodeId) {
  if (!rule) return false;
  if (rule.kind === "NODE_PERFORMING_AGENT" || rule.kind === "NODE_PERFORMING_AGENT_SUPERVISOR") {
    return rule.ref === nodeId;
  }
  return (rule.operands || []).some((o) => ruleRefersToPerformerOf(o, nodeId));
}

/**
 * Enthaelt die Regel (auch verschachtelt) einen Bezug auf den Ausfuehrer eines
 * anderen Schritts? Dann erklaert sich eine leere Bearbeitermenge oft daraus.
 *
 * @param {object} rule Bearbeiterregel
 * @returns {boolean}
 */
function ruleIsRelative(rule) {
  if (!rule) return false;
  if (rule.kind === "NODE_PERFORMING_AGENT" || rule.kind === "NODE_PERFORMING_AGENT_SUPERVISOR") return true;
  return (rule.operands || []).some(ruleIsRelative);
}

function zFindingsPanel() {
  const v = state.validation;
  const zs = v && !v.correct ? v.findings.filter((f) => f.rule[0] === "Z" || f.rule[0] === "A") : [];
  const body = el("div", { class: "panel-b" });
  if (!zs.length) body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Ressourcen/Bearbeiter konsistent (Z/A)."));
  else zs.forEach((f) => body.appendChild(el("div", { class: "finding" }, el("span", { class: "rule" }, f.rule), el("span", null, findingText(f, { withHint: true })))));
  return el("div", { class: "panel" }, el("div", { class: "panel-h" }, el("h2", null, "Ressourcen-Befunde")), body);
}

function addRole() {
  const name = el("input", { type: "text", placeholder: "z. B. Sachbearbeiter" });
  openModal("Rolle", el("label", { class: "field" }, "Name", name), async () => {
    if (!name.value.trim()) return false;
    return commitSchemaChange(() => api.post(orgApi(`/roles`), { name: name.value.trim() }), "Rolle angelegt");
  }, "Anlegen");
}

// Rollen-Panel mit optionalem Gruppenpostfach (Regelgruppe N): die Sammeladresse,
// an die „an das Gruppenpostfach“-Benachrichtigungen (TO_GROUP_MAILBOX) gehen. Der
// Kern prüft Syntax (N1) und – solange ein mail-gebundener Schritt sie braucht –
// Vollständigkeit (N3), lehnt das Entfernen also ggf. mit 422 ab.
function rolesPanel(org, orgEditable) {
  const roles = Object.values(org.roles || {});
  const head = el("div", { class: "panel-h" }, el("h2", null, "Rollen"),
    el("span", { class: "sub" }, "Gruppenpostfach optional"),
    el("span", { class: "spacer", style: "flex:1" }),
    el("button", { class: "btn small", disabled: !orgEditable, onClick: addRole }, "+ Rolle"));
  const body = el("div", { class: "panel-b" });
  if (!roles.length) { body.appendChild(emptyState("Noch keine Rolle.")); return el("div", { class: "panel" }, head, body); }
  const rows = roles.map((r) => {
    const box = r.mailbox ? r.mailbox : el("span", { class: "muted" }, "–");
    const btn = el("button", { class: "btn small", disabled: !orgEditable,
      onClick: () => editRoleMailbox(r) }, "Postfach");
    return [r.name, box, el("div", { style: "display:flex;justify-content:flex-end" }, btn)];
  });
  body.appendChild(table(["Rolle", "Gruppenpostfach", ""], rows));
  return el("div", { class: "panel" }, head, body);
}

// Gruppenpostfach einer Rolle setzen/entfernen (PUT …/roles/{id}/mailbox). Der
// Endpunkt ist unter geteilter wie eingebetteter Organisation gleich benannt.
function editRoleMailbox(role) {
  const mailbox = el("input", { type: "email", value: role.mailbox || "",
    placeholder: "z. B. sachbearbeitung@firma.de" });
  openModal(`Gruppenpostfach – Rolle ${role.name}`, el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      "Sammeladresse für „an das Gruppenpostfach“-Benachrichtigungen. Leer lassen entfernt sie."),
    el("label", { class: "field" }, "E-Mail", mailbox)), async () => {
    return commitSchemaChange(() => api.put(orgApi(`/roles/${role.id}/mailbox`), { mailbox: mailbox.value.trim() || null }), "Gruppenpostfach gespeichert");
  }, "Speichern");
}

// Abteilungspostfach setzen/entfernen. Achtung: der Endpunkt-Pfad unterscheidet
// sich zwischen geteilter (/org-models/{id}/units/…) und eingebetteter
// (/schemas/{id}/org-units/…) Organisation, daher hier eigens aufgelöst.
function editUnitMailbox(unit) {
  const oid = state.schema && state.schema.org_model_id;
  const url = oid
    ? `/org-models/${oid}/units/${unit.id}/mailbox`
    : `/schemas/${state.schemaId}/org-units/${unit.id}/mailbox`;
  const mailbox = el("input", { type: "email", value: unit.mailbox || "",
    placeholder: "z. B. einkauf@firma.de" });
  openModal(`Abteilungspostfach – ${unit.name}`, el("div", null,
    el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
      "Sammeladresse der Abteilung für „an das Gruppenpostfach“-Benachrichtigungen. Leer lassen entfernt sie."),
    el("label", { class: "field" }, "E-Mail", mailbox)), async () => {
    return commitSchemaChange(() => api.put(url, { mailbox: mailbox.value.trim() || null }), "Abteilungspostfach gespeichert");
  }, "Speichern");
}

function addAgent() {
  const org = state.schema.org_model;
  const name = el("input", { type: "text", placeholder: "z. B. Erika Muster" });
  const email = el("input", { type: "email", placeholder: "z. B. erika@firma.de" });
  const roleSel = el("select", { multiple: "multiple", size: Math.min(5, Math.max(1, Object.keys(org.roles).length)) },
    ...Object.values(org.roles).map((r) => el("option", { value: r.id }, r.name)));
  const unitSel = el("select", null, el("option", { value: "" }, "\u2013 keine \u2013"),
    ...Object.values(org.org_units || {}).map((u) => el("option", { value: u.id }, u.name)));
  const depSel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).map((a) => el("option", { value: a.id }, a.name)));
  openModal("Agent", el("div", null,
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field", style: "margin-top:10px" }, "E-Mail (pers\u00f6nliches Postfach)", email),
    el("label", { class: "field", style: "margin-top:10px" }, "Rollen (Mehrfachauswahl)", roleSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Abteilung", unitSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Vertreter", depSel)), async () => {
    if (!name.value.trim()) return false;
    const roleIds = [...roleSel.selectedOptions].map((o) => o.value);
    const payload = { name: name.value.trim(), role_ids: roleIds };
    // Leeres Feld -> keine Adresse (nur mitsenden, wenn gesetzt; N1 pr\u00fcft Syntax).
    if (email.value.trim()) payload.email = email.value.trim();
    if (unitSel.value) payload.org_unit_id = unitSel.value;
    if (depSel.value) payload.deputy_id = depSel.value;
    try { await api.post(orgApi(`/agents`), payload); await refreshSchema(); render(); toast("ok", "Agent angelegt"); }
    catch (err) {
      // A 402 means the agent contingent is full: turn the error into a
      // purchase offer rather than a dead end (only reachable when enforced).
      if (err && err.status === 402) {
        const d = describeError(err);
        toast("err", "Agenten-Kontingent ausgeschöpft", d.lines.length ? d.lines : [d.title]);
        buyAgentPack();
        return false;
      }
      toastError(err); return false;
    }
  }, "Anlegen");
}

function addChildOrgUnit(parentId) {
  const org = state.schema.org_model;
  const name = el("input", { type: "text", placeholder: "z. B. Einkauf" });
  const mgrSel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).map((a) => el("option", { value: a.id }, a.name)));
  const parentName = parentId && org.org_units[parentId] ? org.org_units[parentId].name : "\u2013 oberste Ebene \u2013";
  const parentField = el("input", { type: "text", value: parentName, disabled: "disabled" });
  openModal("Abteilung", el("div", null,
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field", style: "margin-top:10px" }, "\u00DCbergeordnet", parentField),
    el("label", { class: "field", style: "margin-top:10px" }, "Vorgesetzter", mgrSel)), async () => {
    if (!name.value.trim()) return false;
    const payload = { name: name.value.trim() };
    if (parentId) payload.parent_id = parentId;
    if (mgrSel.value) payload.manager_id = mgrSel.value;
    return commitSchemaChange(() => api.post(orgApi(`/org-units`), payload), "Abteilung angelegt");
  }, "Anlegen");
}

function moveOrgUnit(unit) {
  const org = state.schema.org_model;
  // Eigenen Knoten + alle Nachfahren ausschliessen (Zyklus verhindern; Backend prueft zusaetzlich).
  const blocked = new Set([unit.id]);
  let changed = true;
  while (changed) {
    changed = false;
    Object.values(org.org_units).forEach((u) => {
      if (u.parent_id && blocked.has(u.parent_id) && !blocked.has(u.id)) { blocked.add(u.id); changed = true; }
    });
  }
  const sel = el("select", null, el("option", { value: "" }, "\u2013 oberste Ebene \u2013"),
    ...Object.values(org.org_units).filter((u) => !blocked.has(u.id)).map((u) => el("option", { value: u.id }, u.name)));
  if (unit.parent_id) sel.value = unit.parent_id;
  openModal(`Umh\u00E4ngen: ${unit.name}`, el("label", { class: "field" }, "\u00DCbergeordnete Abteilung", sel), async () => {
    return commitSchemaChange(() => api.post(orgApi(`/org-units/${unit.id}/parent`), { parent_id: sel.value || null }), "Abteilung umgeh\u00E4ngt");
  }, "Speichern");
}

function editManager(unit) {
  const org = state.schema.org_model;
  const sel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).map((a) => el("option", { value: a.id }, a.name)));
  if (unit.manager_id) sel.value = unit.manager_id;
  openModal(`Vorgesetzter: ${unit.name}`, el("label", { class: "field" }, "Vorgesetzter", sel), async () => {
    return commitSchemaChange(() => api.post(orgApi(`/org-units/${unit.id}/manager`), { manager_id: sel.value || null }), "Vorgesetzter gesetzt");
  }, "Speichern");
}

function editAgent(agent) {
  const org = state.schema.org_model;
  const name = el("input", { type: "text", value: agent.name });
  const roleSel = el("select", { multiple: "multiple", size: Math.min(5, Math.max(1, Object.keys(org.roles).length)) },
    ...Object.values(org.roles).map((r) => {
      const o = el("option", { value: r.id }, r.name);
      if ((agent.role_ids || []).includes(r.id)) o.selected = true;
      return o;
    }));
  const unitSel = el("select", null, el("option", { value: "" }, "\u2013 keine \u2013"),
    ...Object.values(org.org_units || {}).map((u) => el("option", { value: u.id }, u.name)));
  unitSel.value = agent.org_unit_id || "";
  const email = el("input", { type: "email", value: agent.email || "",
    placeholder: "z. B. erika@firma.de" });
  openModal(`Agent bearbeiten: ${agent.name}`, el("div", null,
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field", style: "margin-top:10px" }, "E-Mail (pers\u00f6nliches Postfach)", email),
    el("label", { class: "field", style: "margin-top:10px" }, "Rollen (Mehrfachauswahl)", roleSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Abteilung", unitSel)), async () => {
    if (!name.value.trim()) return false;
    const roleIds = [...roleSel.selectedOptions].map((o) => o.value);
    // Immer mitsenden: leeres Feld -> null entfernt eine Adresse. Der Kern lehnt
    // das Entfernen ab (N3), solange ein mail-gebundener Schritt sie braucht.
    const payload = { name: name.value.trim(), role_ids: roleIds,
      org_unit_id: unitSel.value || null, email: email.value.trim() || null };
    return commitSchemaChange(() => api.patch(orgApi(`/agents/${agent.id}`), payload), "Agent gespeichert");
  }, "Speichern");
}

function editDeputy(agent) {
  const org = state.schema.org_model;
  const sel = el("select", null, el("option", { value: "" }, "\u2013 keiner \u2013"),
    ...Object.values(org.agents || {}).filter((a) => a.id !== agent.id).map((a) => el("option", { value: a.id }, a.name)));
  if (agent.deputy_id) sel.value = agent.deputy_id;
  openModal(`Vertreter: ${agent.name}`, el("label", { class: "field" }, "Vertreter", sel), async () => {
    return commitSchemaChange(() => api.post(orgApi(`/agents/${agent.id}/deputy`), { deputy_id: sel.value || null }), "Vertreter gesetzt");
  }, "Speichern");
}

// Client-side mirror of the server's suggest_login (vorname.nachname). Only used
// to preview the suggestion; the server remains the source of truth.
function suggestLoginClient(name) {
  const map = { "\u00E4": "ae", "\u00F6": "oe", "\u00FC": "ue", "\u00DF": "ss",
    "\u00C4": "ae", "\u00D6": "oe", "\u00DC": "ue" };
  const translit = (name || "").replace(/[\u00E4\u00F6\u00FC\u00DF\u00C4\u00D6\u00DC]/g, (c) => map[c]);
  const ascii = translit.normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  const parts = ascii.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  return parts.join(".") || "user";
}

// Admin convenience (password mode): provision a login for an agent. The login
// is suggested from the name (server is authoritative); the admin picks the
// coarse RBAC roles. The one-off initial password is shown once afterwards.
function provisionLogin(agent) {
  const roleSel = el("select", { multiple: "multiple", size: 4 },
    ...Object.keys(ROLE_LABELS).map((r) => {
      const o = el("option", { value: r }, ROLE_LABELS[r]);
      if (r === "operator") o.selected = true;
      return o;
    }));
  const loginInput = el("input", { type: "text", placeholder: suggestLoginClient(agent.name) });
  openModal(`Login anlegen: ${agent.name}`, el("div", null,
    el("div", { class: "muted", style: "margin-bottom:10px" },
      "Der Login wird aus dem Namen vorgeschlagen (\u00FCberschreibbar). Es wird ein Initialpasswort erzeugt, das einmalig angezeigt wird; die Person vergibt beim ersten Login ein eigenes Passwort."),
    el("label", { class: "field" }, "Rollen (Mehrfachauswahl)", roleSel),
    el("label", { class: "field", style: "margin-top:10px" }, "Login (optional)", loginInput)), async () => {
    const roles = [...roleSel.selectedOptions].map((o) => o.value);
    if (!roles.length) { toast("err", "Bitte mindestens eine Rolle w\u00E4hlen"); return false; }
    const payload = { agent_id: agent.id, display_name: agent.name, roles };
    if (loginInput.value.trim()) payload.login = loginInput.value.trim();
    try {
      const res = await api.post("/users", payload);
      toast("ok", "Login angelegt", [`Login: ${res.login}`]);
      // Der Zugangsdaten-Dialog ersetzt diesen; ``false``, sonst schloesse
      // openModal ihn sofort wieder (ein Container fuer alle Dialoge).
      showLoginCredentials(res);
      render();   // die Zeile zeigt jetzt „Login: …“ statt des Knopfs
      return false;
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * Nur-Lese-Feld mit Kopierknopf fuer einen einmalig gezeigten Wert
 * (Login, Initialpasswort).
 *
 * Kopiert ueber die Zwischenablage-API; wo sie fehlt oder verweigert wird
 * (unsicherer Kontext, Berechtigung), markiert der Knopf den Text und
 * versucht ``execCommand("copy")``; scheitert auch das, bittet die
 * Beschriftung, selbst zu kopieren. Der Wert bleibt immer sichtbar.
 *
 * @param {string} label Feldbeschriftung
 * @param {string} value anzuzeigender Wert
 * @returns {HTMLElement} das Feld samt Knopf
 */
function copyField(label, value) {
  const input = el("input", { type: "text", value, readonly: "readonly", class: "copy-value",
    "aria-label": label });
  const btn = el("button", { class: "btn small", type: "button", onClick: async () => {
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
        ok = true;
      }
    } catch (_e) { /* Rueckfall unten */ }
    if (!ok) {
      try { input.focus(); input.select(); ok = !!document.execCommand("copy"); } catch (_e) { ok = false; }
    }
    btn.textContent = ok ? "Kopiert \u2713" : "Bitte markieren und kopieren";
  } }, "Kopieren");
  return el("div", { class: "field copy-field" }, el("span", null, label),
    el("div", { class: "copy-row" }, input, btn));
}

/**
 * Zeigt Login und einmaliges Initialpasswort (nach Anlegen oder Zuruecksetzen).
 *
 * Ergebnisdialog (``cancel: false``): ein Knopf „Schliessen“, kein
 * „Abbrechen“ und kein Schliessen per Klick daneben – das Passwort steht nur
 * hier und ist danach nicht wieder abrufbar.
 *
 * @param {{login: string, initial_password: string}} res Antwort von
 *   ``POST /users`` bzw. ``POST /users/{login}/reset-password``
 * @param {string} [title] Dialogtitel (Standard „Zugangsdaten“)
 */
function showLoginCredentials(res, title) {
  openModal(title || "Zugangsdaten", el("div", null,
    el("div", { class: "muted", style: "margin-bottom:10px" },
      "Bitte notieren und der Person sicher mitteilen. Das Initialpasswort wird nur jetzt angezeigt; beim ersten Anmelden vergibt die Person ein eigenes."),
    copyField("Login", res.login),
    copyField("Initialpasswort", res.initial_password)),
    async () => true, "Schlie\u00DFen", { cancel: false });
}


// Bearbeiterregel (BZR) zuweisen. ``fixedNodeId`` (optional) fixiert den Schritt
// fuer den „Bearbeiter zuordnen"-Knopf des Inspektors.
/**
 * Ein Teil einer Bearbeiterregel: Art (Rolle, Abteilung, Person, Bearbeiter
 * bzw. Vorgesetzte:r eines frueheren Schritts) und Bezug.
 *
 * Eigene Funktion, damit ``addStaffRule`` zwei davon verknuepfen kann.
 * Die node-bezogenen Arten bieten die uebrigen Aufgaben-Schritte an; ob der
 * Rueckbezug gueltig ist (Z3), entscheidet der Kern.
 *
 * @param {object} schema das Schema
 * @param {() => (string|null)} currentNode liefert den Zielschritt (nicht als Bezug anbieten)
 * @returns {{node: HTMLElement, read: () => (object|null), refresh: () => void}}
 */
function staffTermPicker(schema, currentNode) {
  const org = schema.org_model || {};
  const refSel = el("select");
  const kindSel = el("select", null,
    el("option", { value: "ROLE" }, "Rolle"),
    el("option", { value: "ORG_UNIT" }, "Abteilung"),
    el("option", { value: "AGENT" }, "Agent (konkrete Person)"),
    el("option", { value: "NODE_PERFORMING_AGENT" }, "Bearbeiter eines Schritts"),
    el("option", { value: "NODE_PERFORMING_AGENT_SUPERVISOR" }, "Vorgesetzte:r des Bearbeiters eines Schritts"));
  const recBox = el("input", { type: "checkbox" });
  const recField = el("label", { class: "field" }, recBox, " Abteilung und alle Bereiche darunter");
  const NODE_KINDS = ["NODE_PERFORMING_AGENT", "NODE_PERFORMING_AGENT_SUPERVISOR"];
  let pending = 0;
  function refresh() {
    clear(refSel);
    if (NODE_KINDS.includes(kindSel.value)) {
      // Nur Schritte, die garantiert vorher laufen -- dieselbe Analyse wie Z3
      // im Kern. Frueher standen auch Schritte des anderen XOR-Zweigs
      // zur Wahl, und erst der Klick brachte die Ablehnung.
      const ticket = ++pending;
      const target = currentNode();
      refSel.appendChild(el("option", { value: "" }, "\u2026 wird geladen"));
      api.get(`/schemas/${schema.id}/nodes/${target}/performer-candidates`)
        .then((ids) => {
          if (ticket !== pending) return;  // inzwischen andere Auswahl
          clear(refSel);
          ids.forEach((id) => {
            const n = schema.nodes[id];
            if (n) refSel.appendChild(el("option", { value: id }, nodeCaption(n)));
          });
          if (!ids.length) refSel.appendChild(el("option", { value: "" }, "(kein Schritt l\u00E4uft sicher vorher)"));
        })
        .catch(() => {
          if (ticket !== pending) return;
          clear(refSel);
          refSel.appendChild(el("option", { value: "" }, "(Schritte nicht abrufbar)"));
        });
    } else {
      const src = kindSel.value === "ROLE" ? org.roles
        : kindSel.value === "AGENT" ? org.agents : org.org_units;
      Object.values(src || {}).forEach((x) => refSel.appendChild(el("option", { value: x.id }, x.name)));
    }
    recField.style.display = kindSel.value === "ORG_UNIT" ? "" : "none";
  }
  kindSel.addEventListener("change", refresh); refresh();
  return {
    node: el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Art", kindSel),
      el("label", { class: "field" }, "Bezug", refSel),
      recField),
    read: () => {
      if (!refSel.value) return null;
      const term = { kind: kindSel.value, ref: refSel.value };
      if (kindSel.value === "ORG_UNIT") term.recursive = recBox.checked;
      return term;
    },
    refresh,
  };
}

/** Verknuepfungen im Regeldialog -- Wert = StaffRuleKind oder "". */
const STAFF_COMBINATORS = [
  { value: "", label: "keine – nur diese Angabe" },
  { value: "AND", label: "UND – muss auch Folgendes erfüllen" },
  { value: "OR", label: "ODER – alternativ auch" },
  { value: "EXCEPT", label: "AUSSER – ohne Folgende" },
];

/**
 * Der erweiterte Bearbeiter-Dialog: eine Angabe oder zwei verknuepfte.
 *
 * Kombinationen (UND/ODER/AUSSER) gab es bis 2026-09 nur ueber die API, obwohl
 * die Modellierer-Anleitung sie im gefuehrten Dialog versprach. Die
 * Regel entsteht hier als ``{kind: AND|OR|EXCEPT, operands: [a, b]}``; ob sie
 * jemanden findet (Z2) oder einen gueltigen Rueckbezug hat (Z3), prueft der
 * Kern und meldet es verstaendlich. Beide Oberflaechen erreichen den Dialog
 * ueber ``bindStaffDialog``; geschrieben wird ueber ``applyStaffRuleTo``.
 *
 * @param {string} [fixedNodeId] Zielschritt; ohne ihn gibt es eine Auswahl
 */
function addStaffRule(fixedNodeId) {
  const schema = state.schema;
  const nodeId = typeof fixedNodeId === "string" ? fixedNodeId : null;
  const nodeSel = nodeId
    ? null
    : el("select", null, ...activitiesOf(schema).map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const target = () => nodeId || (nodeSel && nodeSel.value);
  const first = staffTermPicker(schema, target);
  const second = staffTermPicker(schema, target);
  const combSel = el("select", null, ...STAFF_COMBINATORS.map((c) => el("option", { value: c.value }, c.label)));
  const secondHost = el("div", { class: "staff-term-second" }, second.node);
  const syncComb = () => { secondHost.style.display = combSel.value ? "" : "none"; };
  combSel.addEventListener("change", syncComb); syncComb();
  // Mehrfachzuordnung wie im Dialog der Schritt-Karte -- dieselbe Auswahl,
  // derselbe Schreibweg. Wechselt hier der
  // Schritt, wird die Liste neu gebaut: der gewaehlte Schritt darf nicht
  // zusaetzlich in ihr stehen.
  let others = otherStepsBox(schema, target());
  const othersHost = el("div", null, others.node);
  if (nodeSel) {
    nodeSel.addEventListener("change", () => {
      first.refresh(); second.refresh();
      clear(othersHost);
      others = otherStepsBox(schema, nodeSel.value);
      if (others.node) othersHost.appendChild(others.node);
    });
  }
  openModal("Bearbeiterregel", el("div", { class: "form-grid" },
    nodeId
      // ``wide``: Schritt und Verknuepfung stehen allein in ihrer Zeile --
      // im halben Raster brach der Schrittname um und „AUSSER – ohne
      // Folgende“ war abgeschnitten.
      ? el("div", { class: "field wide" }, "Schritt: ", el("strong", null, nodeCaption(schema.nodes[nodeId])))
      : el("label", { class: "field wide" }, "Schritt", nodeSel),
    first.node,
    el("label", { class: "field wide" }, "Verknüpfen mit", combSel),
    secondHost,
    othersHost), async () => {
    const a = first.read();
    if (!a) { toast("err", "Keine Angabe verf\u00FCgbar"); return false; }
    let rule = a;
    if (combSel.value) {
      const b = second.read();
      if (!b) { toast("err", "Bitte auch die zweite Angabe w\u00E4hlen"); return false; }
      rule = { kind: combSel.value, operands: [a, b] };
    }
    return applyStaffRuleTo(target(), others.selected(), rule);
  }, "Zuordnen");
}

// --------------------------------------------------------------------------
// View: Ausfuehrung
// --------------------------------------------------------------------------

async function viewRun() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;

  const header = el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, schema.name), el("span", { class: "sub" }, `v${schema.version}`), lifecyclePill(schema),
      el("span", { class: "spacer", style: "flex:1" }),
      schema.lifecycle_state === "RELEASED"
        ? el("button", { class: "btn small primary", onClick: startInstance }, "\u25B6 Instanz starten")
        : hasRole("modeler", "admin")
          ? el("button", { class: "btn small", onClick: startInstance }, "\u25B6 Test-Instanz starten")
          : el("span", { class: "muted", style: "font-size:12px" }, "Nur freigegebene Schemata sind instanziierbar.")));
  content.appendChild(header);

  if (!state.instance) {
    content.appendChild(emptyState("Keine Instanz geladen. Starte eine Instanz oder w\u00E4hle eine im Monitoring."));
    return;
  }
  await renderInstanceDetail(content, true);
}

async function startInstance() {
  try {
    const inst = await api.post(`/schemas/${state.schemaId}/instances`);
    await loadInstance(inst.id);
    render();
    toast("ok", inst.is_test ? "Test-Instanz gestartet" : "Instanz gestartet",
      [`${instanceCaption(inst, state.schema)} \u00B7 ${inst.id}`]);
  } catch (err) { toastError(err); }
}

async function loadInstance(id) {
  state.instanceId = id;
  state.instance = await api.get(`/instances/${id}`);
  state.worklist = await api.get(`/instances/${id}/worklist`);
  rememberInstance(id);
}

/** sessionStorage-Schluessel des gewaehlten Vorgangs (siehe ``rememberInstance``). */
const INSTANCE_KEY = "selectedInstance";

/**
 * Merkt den gewaehlten Vorgang fuer ein Neuladen **dieses Tabs**.
 *
 * Nach F5 stand „Keine Instanz geladen“, obwohl man gerade an einem Vorgang
 * war. Gemerkt wird in ``sessionStorage`` (nur dieser Tab, endet mit ihm) und
 * **zusammen mit dem Login**: Ein Vorgang ist personenbezogen -- die naechste
 * Anmeldung darf ihn nie erben, auch nicht ueber einen anderen Tab, der sich
 * inzwischen als jemand anderes angemeldet hat. ``resetSessionState`` und
 * ``dropUnreadableInstance`` loeschen den Eintrag.
 * @param {string} id Vorgang
 */
function rememberInstance(id) {
  const owner = (state.principal && state.principal.subject) || "";
  storageSet(sessionStorage, INSTANCE_KEY, JSON.stringify({ owner, id }));
}

/** Vergisst den gemerkten Vorgang (Abmelden, Login-Wechsel, nicht mehr lesbar). */
function forgetInstance() {
  try { sessionStorage.removeItem(INSTANCE_KEY); } catch (_e) { /* nur Komfort */ }
}

/**
 * Laedt beim Start den gemerkten Vorgang wieder -- nur fuer denselben Login.
 *
 * Gehoert der Eintrag einem anderen Login (oder ist er unlesbar), wird er
 * verworfen und nichts geladen. Verweigert der Kern den Vorgang (404/403:
 * geloescht oder nicht mehr einsehbar), erscheint der Hinweis aus
 * ``dropUnreadableInstance``; bei anderen Fehlern bleibt die Auswahl leer.
 * Der Prozess des Vorgangs wird mitgewaehlt, damit die Ausfuehrung ihn zeigt.
 * @returns {Promise<boolean>} true, wenn ein Vorgang wiederhergestellt wurde
 */
async function restoreInstance() {
  if (state.instanceId) return false;
  let saved = null;
  try { saved = JSON.parse(storageGet(sessionStorage, INSTANCE_KEY) || "null"); } catch (_e) { saved = null; }
  const owner = (state.principal && state.principal.subject) || "";
  if (!saved || !saved.id || saved.owner !== owner) { forgetInstance(); return false; }
  try {
    await loadInstance(saved.id);
  } catch (err) {
    if (!dropUnreadableInstance(err)) { state.instanceId = null; state.instance = null; state.worklist = null; }
    forgetInstance();
    return false;
  }
  if (state.instance.schema_id !== state.schemaId) {
    // Ein Fehler hier darf den Start nicht abbrechen (sonst liefe etwa die
    // Live-Aktualisierung nie an): dann eben ohne wiederhergestellten Vorgang.
    const previous = state.schemaId;
    try {
      state.schemaId = state.instance.schema_id;
      await refreshSchema();
      renderSchemaPicker();
    } catch (_e) {
      // Zurueck zum vorher gewaehlten Prozess und ihn vollstaendig neu laden
      // (Schema, Pruefergebnis, gemerkte Auswahl) -- ``refreshSchema`` kann
      // schon teilweise umgestellt haben. Scheitert auch das, bleibt der Start
      // trotzdem stehen.
      state.schemaId = previous;
      try { await refreshSchema(); renderSchemaPicker(); } catch (_e2) { /* bestmoeglich */ }
      state.instanceId = null; state.instance = null; state.worklist = null;
      forgetInstance();
      return false;
    }
  }
  return true;
}

async function renderInstanceDetail(container, withActions) {
  const inst = state.instance;
  const wl = state.worklist;
  // Schema, gegen das die Instanz laeuft (ggf. Ad-hoc-Variante)
  const runSchema = inst.ad_hoc_schema || state.schema;
  const statePill = statePillFor(inst.state);

  const graphPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Live-Prozesslandkarte"), el("span", { class: "sub", title: `Kennung: ${inst.id}` }, instanceCaption(inst, runSchema)), statePill,
      inst.is_test ? el("span", { class: "pill pill-amber", title: "Test-Instanz \u2013 nicht im Monitoring gez\u00E4hlt" }, "TEST") : null),
    el("div", { class: "panel-b" }, renderGraph(runSchema, { instance: inst })));

  // Worklist
  const wlBody = el("div", { class: "panel-b" });
  // Zustaendigkeit je bereitem Schritt (aus der Bearbeiterregel, inkl.
  // Vertretung). Die Instanz-Sicht zeigt weiter ALLE bereiten Schritte (E1:
  // Transparenz), aber sie sagt, wer zustaendig ist, und bietet das Abschliessen
  // nur passend an -- vorher stand bei jedem Schritt „Abschliessen“, egal fuer wen.
  const eligibleOf = {};
  if (inst.state !== "COMPLETED") {
    try {
      (await api.get(`/instances/${inst.id}/tasks`)).forEach((t) => { eligibleOf[t.node_id] = t.eligible_agents || []; });
    } catch (e) { /* best effort: ohne Liste bleibt die bisherige Anzeige */ }
  }
  if (inst.state === "COMPLETED") {
    wlBody.appendChild(el("div", { class: "ok-banner" }, "\u2713 Instanz abgeschlossen \u2013 jeder Schritt ist erledigt oder \u00FCbersprungen."));
  } else {
    (wl.ready_activities || []).forEach((nid) => {
      const node = runSchema.nodes[nid];
      // E1/E2: Transparenz statt Verstecken \u2013 die Instanz-Sicht zeigt alle
      // bereiten Schritte samt Inhaber und Detailzustand (Pause/Scheitern
      // inkl. Begr\u00FCndung); die pers\u00F6nlichen Listen filtern.
      const owner = (inst.claimed_by || {})[nid];
      const nodeDetail = (inst.node_details || {})[nid];
      const detailTag = nodeDetail === "SUSPENDED"
        ? el("span", { class: "tag" }, "angehalten")
        : nodeDetail === "FAILED"
          ? el("span", { class: "tag" }, "gescheitert: " + ((inst.node_detail_reason || {})[nid] || "ohne Begr\u00FCndung"))
          : null;
      const action = withActions ? completionActionFor(inst, nid, node, eligibleOf[nid], runSchema) : null;
      wlBody.appendChild(el("div", { class: "worklist-item" },
        el("span", { class: "name" }, node ? nodeCaption(node) : nid),
        owner
          ? el("span", { class: "tag" }, "\u00FCbernommen von " + agentNameOf(owner))
          : el("span", { class: "tag" }, "bereit"),
        eligibleOf[nid] && eligibleOf[nid].length
          ? el("span", { class: "tag muted", title: eligibleOf[nid].map(agentNameOf).join(", ") }, "zust\u00E4ndig: " + responsibleSummary(eligibleOf[nid]))
          : unstaffedTag(runSchema, nid),
        detailTag,
        action));
    });
    if (!(wl.ready_activities || []).length) {
      wlBody.appendChild(el("div", { class: "muted", style: "font-size:13px" }, "Keine bereiten Schritte (l\u00E4uft automatisch weiter oder wartet auf Teilprozess)."));
    }
  }
  const wlPanel = el("div", { class: "panel" }, el("div", { class: "panel-h" }, el("h2", null, "Arbeitsliste")), wlBody);

  // Datenwerte
  const dataRows = Object.entries(inst.data_values || {}).map(([k, v]) => {
    const elem = runSchema.data_elements[k];
    return [elem ? elem.name : k, formatValue(elem, v)];
  });
  // Daten koennen direkt nach dem Start eingegeben werden – unabhaengig davon,
  // ob schon eine Aktivitaet aktiviert wurde.
  // „Daten eingeben“ nur, wo der Kern es grundsaetzlich erlaubt (Spiegel von
  // _authorize_data_write, nur Sichtbarkeit): Schreibrolle vorausgesetzt;
  // dann Modellierer/Admin, jeder Test-Vorgang, wer fuer andere handeln darf
  // (offener/Token-Modus, Rolle integration) oder eine Person mit eigenem
  // offenem Schritt. Den Einzelfall entscheidet weiter der Kern.
  const meAgent = state.principal && state.principal.agent_id;
  const ownOpenStep = !!meAgent && hasRole("operator")
    && Object.values(eligibleOf || {}).some((ids) => (ids || []).includes(meAgent));
  const canEditData = withActions && inst.state !== "COMPLETED"
    && hasRole("operator", "modeler", "admin")
    && (hasRole("modeler", "admin") || inst.is_test || mayActForOthers() || ownOpenStep)
    && Object.values(runSchema.data_elements || {}).some((e) => e.source !== "EXTERNAL");
  const dataPanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Instanzdaten"),
      canEditData ? el("span", { class: "spacer", style: "flex:1" }) : null,
      canEditData ? el("button", { class: "btn small", onClick: () => openInstanceDataForm(runSchema, inst) }, "Daten eingeben") : null),
    el("div", { class: "panel-b" }, dataRows.length ? table(["Element", "Wert"], dataRows) : emptyState("Noch keine Werte.")));

  // Audit-Timeline (Schritt 15)
  let events = [];
  try { events = await api.get(`/instances/${inst.id}/audit`); } catch (e) { /* ignore */ }
  const tlBody = el("div", { class: "panel-b" });
  if (!events.length) {
    tlBody.appendChild(emptyState("Noch keine Ereignisse aufgezeichnet."));
  } else {
    const tl = el("div", { class: "timeline" });
    // Spaltenkopf: der Bearbeiter (Akteur) steht in einer eigenen Spalte.
    tl.appendChild(el("div", { class: "tl-item tl-head" },
      el("span", { class: "tl-time" }, "Zeit"),
      el("span", { class: "tl-type" }, "Ereignis"),
      el("span", { class: "tl-actor" }, "Bearbeiter"),
      el("span", { class: "tl-meta" }, "Detail")));
    events.forEach((ev) => {
      tl.appendChild(el("div", { class: "tl-item" },
        el("span", { class: "tl-time" }, fmtTimestamp(ev.timestamp)),
        el("span", { class: "tl-type" }, eventLabel(ev.event_type)),
        el("span", { class: "tl-actor" }, auditActorLabel(ev)),
        el("span", { class: "tl-meta" }, auditDetailText(ev))));
    });
    tlBody.appendChild(tl);
  }
  const timelinePanel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Audit-Verlauf"), el("span", { class: "sub" }, countLabel(events.length, "Ereignis", "Ereignisse"))),
    tlBody);

  // Ad-hoc-Instanzanpassung (Schema-Evolution einer einzelnen Instanz, R1/R2).
  // Der Modellierer darf eine laufende Instanz an die Realitaet anpassen, ohne
  // das freigegebene Schema zu aendern. Angeboten werden nur Aenderungen im
  // noch nicht ausgefuehrten Bereich (R1); der Kern prueft zusaetzlich R2.
  const adhocPanel = (withActions && hasRole("modeler", "admin") && inst.state !== "COMPLETED")
    ? renderAdhocPanel(runSchema, inst)
    : null;

  // Neuere freigegebene Version vorhanden? Dann Migration anbieten (derselbe
  // Assistent wie in der Modellieren-Sicht, auf diese eine Instanz beschraenkt).
  const migratePanel = (withActions && hasRole("operator", "modeler", "admin")
      && inst.state === "RUNNING" && !inst.is_test && !inst.parent_instance_id)
    ? await instanceMigrationPanel(inst)
    : null;

  container.appendChild(el("div", { class: "grid-2" }, graphPanel, el("div", null, migratePanel, wlPanel, dataPanel, adhocPanel, timelinePanel)));
}

// Panel fuer die Ad-hoc-Anpassung einer einzelnen Instanz. Die Auswahl der
// erlaubten Ziele spiegelt R1 aus procworks.adhoc wider (nur der noch nicht
// ausgefuehrte Bereich ist aenderbar); der Kern setzt R1 und R2 verbindlich
// durch, die UI bietet nur zulaessige Aktionen vorab an.
function renderAdhocPanel(schema, inst) {
  const nodes = schema.nodes || {};
  const reached = (nid) => {
    const s = (inst.node_states || {})[nid];
    return s !== undefined && s !== "NOT_ACTIVATED";
  };
  const edgeSignaled = (source, target) => {
    const st = (inst.edge_states || {})[`${source}->${target}`];
    return st !== undefined && st !== "NOT_SIGNALED";
  };
  const out = (nid) => (schema.edges || []).filter((e) => e.source === nid);
  const inc = (nid) => (schema.edges || []).filter((e) => e.target === nid);

  // R1 fuer Einfuegen: Anker != END, genau eine ausgehende, noch nicht
  // signalisierte Kante, Nachfolger noch nicht erreicht.
  const insertAnchors = Object.values(nodes).filter((n) => {
    if (n.type === "END") return false;
    const o = out(n.id);
    if (o.length !== 1) return false;
    const succ = o[0].target;
    return !edgeSignaled(n.id, succ) && !reached(succ);
  });
  // R1 fuer Umbenennen: ACTIVITY/SUBPROCESS, noch nicht erreicht.
  const renameTargets = Object.values(nodes).filter(
    (n) => (n.type === "ACTIVITY" || n.type === "SUBPROCESS") && !reached(n.id));
  // R1 fuer Entfernen: serielle ACTIVITY (eine rein/eine raus), noch nicht erreicht.
  const deleteTargets = Object.values(nodes).filter(
    (n) => n.type === "ACTIVITY" && !reached(n.id) && inc(n.id).length === 1 && out(n.id).length === 1);

  const body = el("div", { class: "panel-b" });
  body.appendChild(el("p", { class: "muted", style: "font-size:13px;margin-top:0" },
    "Passt diese eine Instanz an die Realit\u00E4t an, ohne das freigegebene Schema zu \u00E4ndern. " +
    "Nur der noch nicht ausgef\u00FChrte Bereich ist \u00E4nderbar (R1); jede \u00C4nderung wird vor der \u00DCbernahme auf Korrektheit gepr\u00FCft (R2)."));
  body.appendChild(el("div", { class: "btn-row" },
    el("button", { class: "btn small", disabled: insertAnchors.length ? null : "disabled",
      onClick: () => openAdhocInsert(schema, inst, insertAnchors) }, "Schritt einf\u00FCgen"),
    el("button", { class: "btn small", disabled: renameTargets.length ? null : "disabled",
      onClick: () => openAdhocRename(schema, inst, renameTargets) }, "Schritt umbenennen"),
    el("button", { class: "btn small danger", disabled: deleteTargets.length ? null : "disabled",
      onClick: () => openAdhocDelete(schema, inst, deleteTargets) }, "Schritt entfernen")));

  const deltas = inst.ad_hoc_deltas || [];
  if (deltas.length) {
    const list = el("ul", { class: "adhoc-deltas" }, ...deltas.map((d) => el("li", null, d)));
    body.appendChild(el("div", { class: "adhoc-log" },
      el("div", { class: "sub", style: "margin:8px 0 4px" }, "Angewendete Anpassungen"), list));
  }

  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Instanz anpassen (Ad-hoc)")), body);
}

async function reloadInstance(instId) {
  await loadInstance(instId);
  render();
}

/**
 * Bearbeiterregel, die ein ad hoc eingefuegter Schritt als Vorschlag erbt: die
 * des Ankers bzw. -- ist der Anker ein Gateway oder ohne Regel -- die des
 * naechsten Schritts davor (Rueckweg ueber die erste eingehende Kante).
 * Reine Vorbelegung; ob die Regel passt, prueft der Kern (Z1-Z3, B2).
 * @param {object} schema wirksames Schema der Instanz
 * @param {string} anchorId Knoten, hinter dem eingefuegt wird
 * @returns {{rule: object, from: string}|null} Regel und Schrittname oder null
 */
function adhocSuggestedRule(schema, anchorId) {
  const rules = schema.staff_rules || {};
  const seen = new Set();
  let current = anchorId;
  while (current && !seen.has(current)) {
    seen.add(current);
    if (rules[current]) {
      const n = (schema.nodes || {})[current];
      return { rule: rules[current], from: n ? nodeCaption(n) : current };
    }
    const inc = controlEdges(schema).filter((e) => e.target === current);
    current = inc.length ? inc[0].source : null;
  }
  return null;
}

/**
 * Eingabefeld „Anlass" fuer eine Ad-hoc-Aenderung.
 *
 * Eine Ad-hoc-Aenderung legt fest, wie *dieser eine* laufende Vorgang
 * weitergeht; der Kern verlangt deshalb fuer echte Vorgaenge einen Anlass und
 * schreibt ihn mit dem Namen der handelnden Person in den Verlauf. Test-
 * Instanzen schreiben keinen Verlauf -- dort bleibt das Feld freiwillig.
 * @param {object} inst die Instanz, die geaendert wird
 * @returns {{input: HTMLInputElement, field: HTMLElement, value: function(): (string|null)}}
 *   ``value()`` liefert den Anlass oder ``null``, wenn er fehlt, obwohl er
 *   Pflicht ist (dann ist bereits ein Hinweis angezeigt).
 */
function adhocReasonField(inst) {
  const required = !inst.is_test;
  const input = el("input", { type: "text",
    placeholder: required ? "z. B. Kunde verlangt Zusatzpr\u00FCfung" : "optional" });
  const field = el("label", { class: "field" }, required ? "Anlass *" : "Anlass", input);
  const value = () => {
    const text = input.value.trim();
    if (required && !text) {
      toast("err", "Bitte einen Anlass angeben", ["Er steht mit deinem Namen im Verlauf des Vorgangs."]);
      return null;
    }
    return text;
  };
  return { input, field, value };
}

// Ad-hoc: neuen seriellen Schritt hinter einem Anker einfuegen.
//
// Der Schritt braucht eine Bearbeiterregel (B2 im Ad-hoc-Pfad): ohne
// sie stand er zur Laufzeit in keiner Arbeitsliste, und der Vorgang kam nur per
// Aufsichtseingriff weiter. Vorbelegt ist die Regel des Schritts davor
// (``adhocSuggestedRule``); daneben Rollen, Abteilungen und Personen des
// Organisationsmodells. Die Entscheidung trifft der Kern.
function openAdhocInsert(schema, inst, anchors) {
  const anchorSel = el("select", null,
    ...anchors.map((n) => el("option", { value: n.id }, nodeCaptionInContext(schema, n))));
  const labelInput = el("input", { type: "text", placeholder: "Bezeichnung des neuen Schritts" });
  const org = schema.org_model || { roles: {}, org_units: {}, agents: {} };
  const choices = {};
  const staffSel = el("select");
  const fillStaff = () => {
    staffSel.innerHTML = "";
    Object.keys(choices).forEach((k) => delete choices[k]);
    const add = (group, key, label, rule) => {
      choices[key] = rule;
      group.appendChild(el("option", { value: key }, label));
    };
    const suggestion = adhocSuggestedRule(schema, anchorSel.value);
    staffSel.appendChild(el("option", { value: "" }, "\u2013 bitte w\u00E4hlen \u2013"));
    if (suggestion) {
      const g = el("optgroup", { label: "Vorschlag" });
      add(g, "SUGGEST", `Wie \u201E${suggestion.from}\u201C: ${describeRule(suggestion.rule, schema)}`, suggestion.rule);
      staffSel.appendChild(g);
    }
    [["Rollen", "ROLE", org.roles], ["Abteilungen", "ORG_UNIT", org.org_units], ["Personen", "AGENT", org.agents]]
      .forEach(([title, kind, entries]) => {
        const list = Object.values(entries || {});
        if (!list.length) return;
        const g = el("optgroup", { label: title });
        list.forEach((x) => add(g, `${kind}:${x.id}`, x.name, { kind, ref: x.id }));
        staffSel.appendChild(g);
      });
    staffSel.value = suggestion ? "SUGGEST" : "";
  };
  anchorSel.addEventListener("change", fillStaff);
  fillStaff();
  const reason = adhocReasonField(inst);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Einf\u00FCgen hinter", anchorSel),
    el("label", { class: "field" }, "Neuer Schritt", labelInput),
    el("label", { class: "field" }, "Bearbeiter *", staffSel),
    reason.field);
  openModal("Schritt einf\u00FCgen (Ad-hoc)", body, async () => {
    const label = labelInput.value.trim();
    if (!label) { toast("info", "Bitte eine Bezeichnung angeben."); return false; }
    const rule = choices[staffSel.value];
    if (!rule) { toast("err", "Bitte festlegen, wer den neuen Schritt bearbeitet."); return false; }
    const why = reason.value();
    if (why === null) return false;
    const payload = { after_node_id: anchorSel.value, label, staff_rule: rule };
    if (why) payload.reason = why;
    try {
      await api.post(`/instances/${inst.id}/adhoc/insert`, payload);
      toast("ok", "Schritt eingef\u00FCgt", [describeRule(rule, schema)]);
      await reloadInstance(inst.id);
    } catch (err) { toastError(err); return false; }
  }, "Einf\u00FCgen");
}

// Ad-hoc: noch nicht erreichten Schritt umbenennen.
function openAdhocRename(schema, inst, targets) {
  const targetSel = el("select", null,
    ...targets.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const labelInput = el("input", { type: "text", placeholder: "Neue Bezeichnung" });
  const syncLabel = () => {
    const n = schema.nodes[targetSel.value];
    labelInput.value = n ? (n.label || "") : "";
  };
  targetSel.addEventListener("change", syncLabel);
  syncLabel();
  const reason = adhocReasonField(inst);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Schritt", targetSel),
    el("label", { class: "field" }, "Neue Bezeichnung", labelInput),
    reason.field);
  openModal("Schritt umbenennen (Ad-hoc)", body, async () => {
    const label = labelInput.value.trim();
    if (!label) { toast("info", "Bitte eine Bezeichnung angeben."); return false; }
    const why = reason.value();
    if (why === null) return false;
    try {
      await api.post(`/instances/${inst.id}/adhoc/rename`,
        why ? { node_id: targetSel.value, label, reason: why } : { node_id: targetSel.value, label });
      toast("ok", "Schritt umbenannt");
      await reloadInstance(inst.id);
    } catch (err) { toastError(err); return false; }
  }, "Umbenennen");
}

// Ad-hoc: noch nicht erreichten seriellen Schritt entfernen.
function openAdhocDelete(schema, inst, targets) {
  const targetSel = el("select", null,
    ...targets.map((n) => el("option", { value: n.id }, nodeCaption(n))));
  const reason = adhocReasonField(inst);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Zu entfernender Schritt", targetSel),
    reason.field,
    el("p", { class: "muted", style: "font-size:13px" },
      "Vorg\u00E4nger und Nachfolger werden wieder direkt verbunden."));
  openModal("Schritt entfernen (Ad-hoc)", body, async () => {
    const why = reason.value();
    if (why === null) return false;
    try {
      await api.post(`/instances/${inst.id}/adhoc/delete`,
        why ? { node_id: targetSel.value, reason: why } : { node_id: targetSel.value });
      toast("ok", "Schritt entfernt");
      await reloadInstance(inst.id);
    } catch (err) { toastError(err); return false; }
  }, "Entfernen", { danger: true });
}

/**
 * Warnzeichen fuer einen bereiten Schritt, dessen Bearbeiterregel niemanden
 * findet.
 *
 * Ohne es saehe dieser Fall aus wie ein Schritt ganz ohne Bearbeiterregel: kein
 * „zustaendig"-Eintrag, ein gruenes „Abschliessen" -- und der Schritt taucht in
 * keiner persoenlichen Arbeitsliste auf, ohne dass irgendwo stand, warum. Der
 * haeufigste Grund ist eine relative Regel (Vier-Augen), deren Bezugsschritt per
 * Aufsichtseingriff ohne Bearbeiter erledigt wurde; der Hinweistext nennt genau
 * das, wenn er zutrifft.
 *
 * @param {object} schema Schema der Instanz
 * @param {string} nid Knoten-ID des bereiten Schritts
 * @returns {HTMLElement|null} das Zeichen, oder null ohne Bearbeiterregel
 */
function unstaffedTag(schema, nid) {
  const rule = ((schema || {}).staff_rules || {})[nid];
  if (!rule) return null;  // Automatik-/unbesetzter Schritt: kein Widerspruch
  const why = ruleIsRelative(rule)
    ? " Sie bezieht sich auf die Person eines früheren Schritts – wurde dieser als Aufsichtseingriff ohne Bearbeiter abgeschlossen, gibt es diese Person nicht."
    : " Keiner der in Frage kommenden Bearbeiter ist derzeit im Organisationsmodell zu finden.";
  return el("span", { class: "tag warn", title: `Bearbeiterregel: ${describeRule(rule, schema)}.${why} Nur eine Aufsicht kann den Schritt abschließen.` },
    "niemand zuständig");
}

/**
 * Kurzform der Zustaendigen: bis zu zwei Namen, sonst „Name, Name +N“.
 * @param {string[]} agentIds zustaendige Agenten
 * @returns {string}
 */
function responsibleSummary(agentIds) {
  const names = agentIds.map(agentNameOf);
  return names.length <= 2 ? names.join(", ") : `${names.slice(0, 2).join(", ")} +${names.length - 2}`;
}

/**
 * Abschliessen-Knopf eines bereiten Schritts in der Instanz-Sicht, passend zur Rolle.
 *
 * * An eine Person gebunden und zustaendig (oder Schritt ohne Bearbeiterregel):
 *   „Abschliessen“.
 * * Gebunden, aber nicht zustaendig: kein Knopf, sondern ein Hinweis -- der Kern
 *   lehnte den Abschluss ohnehin ab (409).
 * * Nicht gebunden (Modellierer/Administrator) bei einem Schritt mit
 *   Bearbeiterregel: „Als Aufsicht abschliessen“. Der Kern verlangt dann eine
 *   Begruendung und protokolliert den Eingriff; bei aktiver Lizenzierung lehnt er
 *   ihn ab. Test-Instanzen sind davon ausgenommen (normales „Abschliessen“).
 *
 * Der Client entscheidet nichts endgueltig -- er waehlt nur die passende Form.
 *
 * Sonderfall **Regel ohne Zustaendige**: Traegt
 * der Schritt eine Bearbeiterregel, loest sie aber gerade niemanden auf -- etwa
 * eine Vier-Augen-Regel, deren Vorschritt per Aufsichtseingriff ohne Bearbeiter
 * erledigt wurde --, dann stand hier bisher das gruene "Abschliessen", als waere
 * alles in Ordnung. Jetzt heisst es auch dort "Als Aufsicht abschliessen"; einer
 * gebundenen Person wird kein Knopf angeboten, den der Kern mit 409 abweisen
 * wuerde.
 *
 * @param {object} inst die Instanz
 * @param {string} nid Knoten-ID
 * @param {object} node der Knoten
 * @param {string[]|undefined} eligible zustaendige Agenten (undefined = unbekannt)
 * @param {object} [schema] Schema der Instanz (fuer die Bearbeiterregel)
 * @returns {HTMLElement}
 */
function completionActionFor(inst, nid, node, eligible, schema) {
  // Abschliessen (auch als Aufsicht) erlaubt der Kern nur Bearbeitern,
  // Modellierern und Administratoren. Ein Leser bekam die Knoepfe trotzdem
  // und scheiterte erst nach dem Ausfuellen der Maske mit 403.
  if (!hasRole("operator", "modeler", "admin")) return null;
  const me = state.principal && state.principal.agent_id;
  const staffed = eligible && eligible.length > 0;
  const ruled = !!((schema || {}).staff_rules || {})[nid];
  if (me && staffed && !eligible.includes(me)) {
    return el("span", { class: "tag muted" }, "nicht deine Aufgabe");
  }
  if (ruled && !staffed && !inst.is_test) {
    if (me) return el("span", { class: "tag muted" }, "nur per Aufsicht abschlie\u00DFbar");
    return el("button", { class: "btn small",
      title: "Die Bearbeiterregel dieses Schritts findet aktuell niemanden \u2013 der Abschluss wird als Aufsichtseingriff mit Begr\u00FCndung protokolliert",
      onClick: () => completeActivity(nid, node) }, "Als Aufsicht abschlie\u00DFen");
  }
  if (!me && staffed && !inst.is_test) {
    return el("button", { class: "btn small",
      title: "Du bist keinem Bearbeiter zugeordnet – der Abschluss wird als Aufsichtseingriff mit Begründung protokolliert",
      onClick: () => completeActivity(nid, node) }, "Als Aufsicht abschlie\u00DFen");
  }
  return el("button", { class: "btn small green", onClick: () => completeActivity(nid, node) }, "Abschlie\u00DFen");
}

async function completeActivity(nodeId, node) {
  const schema = state.instance.ad_hoc_schema || state.schema;
  await promptComplete(schema, state.instanceId, nodeId, node ? nodeCaption(node) : nodeId, null,
    async () => { await loadInstance(state.instanceId); render(); },
    state.instance.data_values);
}

// Instanzdaten direkt eingeben/aendern – ohne eine Aktivitaet abzuschliessen.
// Angeboten werden alle INSTANCE-Datenelemente des Schemas (EXTERNAL-Elemente
// werden zur Laufzeit ueber Connectoren aufgeloest und daher nicht abgefragt).
//
// Wer was aendern darf, entscheidet allein der Kern: ein Bearbeiter
// nur die Werte seines eigenen offenen Schritts (sonst 403 mit Erklaerung),
// Modellierer/Admin alles Uebrige als Aufsichtseingriff mit Begruendung. Der
// Client kennt die Regel nicht; er reagiert auf die 422 „Aufsichtseingriff…"
// mit der Frage nach der Begruendung und sendet erneut. Die Felder kommen aus
// derselben Widget-Factory wie die Aufgabenmaske (``maskControl`` +
// ``fallbackWidget``) -- frueher wandelte der Dialog Ja/Nein-Freitext selbst
// und still in ``false``.
function openInstanceDataForm(schema, inst) {
  const elems = Object.values(schema.data_elements || {}).filter((e) => e.source !== "EXTERNAL");
  if (!elems.length) { toast("info", "Keine Instanz-Datenelemente definiert."); return; }
  const inputs = {};
  const body = el("div", { class: "form-grid" });
  elems.forEach((elem) => {
    const cur = (inst.data_values || {})[elem.id];
    const { control, read } = maskControl(elem, fallbackWidget(elem), null, cur);
    inputs[elem.id] = { read, cur };
    body.appendChild(el("label", { class: "field" }, elem.name, control));
  });
  const submit = async (values, reason) => {
    try {
      const payload = { values };
      if (reason) payload.reason = reason;
      await api.put(`/instances/${inst.id}/data`, payload);
      toast("ok", "Instanzdaten gespeichert");
      await loadInstance(inst.id);
      render();
    } catch (err) {
      if (isSupervisionRequired(err)) { askDataCorrectionReason((text) => submit(values, text)); return false; }
      toastError(err); return false;
    }
  };
  openModal("Instanzdaten eingeben", body, async () => {
    const values = {};
    for (const [eid, { read, cur }] of Object.entries(inputs)) {
      const val = read();
      if (val === undefined || val === cur) continue;
      values[eid] = val;
    }
    if (!Object.keys(values).length) { toast("info", "Keine ge\u00E4nderten Werte."); return; }
    return submit(values, null);
  }, "Speichern");
}

/**
 * Fragt die Begruendung einer Datenkorrektur ab (Aufsichtseingriff):
 * Die Werte gehoeren zu keinem Schritt, den der Login gerade bearbeitet. Die
 * Begruendung steht danach im Audit-Verlauf neben altem und neuem Wert.
 * @param {(reason: string) => Promise<unknown>} onConfirm sendet erneut mit Begruendung
 */
function askDataCorrectionReason(onConfirm) {
  const reason = el("textarea", { rows: "3", placeholder: "z. B. Tippfehler im Antrag, R\u00FCcksprache mit der Antragstellerin" });
  const body = el("div", { class: "form-grid" },
    el("div", { class: "card-hint" },
      "Diese Werte geh\u00F6ren zu keinem Schritt, den du gerade bearbeitest. Die \u00C4nderung wird als Aufsichtseingriff mit altem und neuem Wert und deiner Begr\u00FCndung im Audit-Verlauf festgehalten."),
    el("label", { class: "field" }, "Begr\u00FCndung *", reason));
  openModal("Daten korrigieren", body, async () => {
    const text = reason.value.trim();
    if (!text) { toast("err", "Bitte eine Begr\u00FCndung angeben"); return false; }
    return onConfirm(text);
  }, "Korrektur speichern");
}

// Schliesst einen Schritt ueber seine Eingabemaske ab.
//
// ``dataValues`` sind die aktuellen Instanzdaten (``ProcessInstance.data_values``)
// und belegen jedes Feld vor, dessen Element schon einen Wert traegt. Das ist fuer
// datenreiche Prozesse wesentlich und nicht bloss Komfort: Ein Nur-Lese-Feld ist
// per Definition die *Entscheidungsgrundlage* des Schritts (der Auftragswert bei
// der Freigabe, der Rechnungsbetrag beim Zahlungsabgleich) -- ohne Vorbelegung
// bliebe es leer und der Schritt waere fachlich nicht entscheidbar. Auch
// Schreibfelder starten mit dem bisherigen Wert, damit eine Uebernahme aus einem
// Vor- oder Elternprozess nur bestaetigt statt abgetippt werden muss.
/**
 * Ist die Ablehnung die Aufforderung zum Aufsichtseingriff?
 * Erkennt die 422-Antwort des Kerns, die eine Begruendung verlangt.
 * @param {{status?: number, detail?: unknown}} err abgefangener Request-Fehler
 * @returns {boolean}
 */
function isSupervisionRequired(err) {
  return !!err && err.status === 422 && typeof err.detail === "string" && err.detail.startsWith("Aufsichtseingriff");
}

/**
 * Fragt die Pflichtbegruendung eines Aufsichtseingriffs ab. Der Dialog erklaert,
 * warum (kein Bearbeiter hinter dem Login) und dass die Angabe im Audit steht.
 *
 * Er warnt ausserdem **vorher**, wenn spaetere Schritte ihre Bearbeiter relativ
 * zu diesem hier bestimmen (Vier-Augen-Prinzip): Ein Aufsichtseingriff
 * hinterlaesst keinen Ausfuehrer, an dem eine solche Regel ansetzen koennte --
 * der Folgeschritt landet dann in keiner Arbeitsliste. Die Warnung kommt
 * deshalb vorher, nicht erst, wenn es zu spaet ist.
 *
 * @param {string} label Bezeichnung des Schritts
 * @param {(reason: string) => Promise<unknown>} onConfirm sendet den Abschluss mit Begruendung
 * @param {object} [schema] Schema der Instanz (fuer die Folgewirkungs-Warnung)
 * @param {string} [nodeId] Knoten-ID des Schritts
 */
function askSupervisionReason(label, onConfirm, schema, nodeId) {
  const reason = el("textarea", { rows: "3", placeholder: "z. B. Bearbeiterin krank, Frist l\u00E4uft ab" });
  const dependants = Object.entries(((schema || {}).staff_rules) || {})
    .filter(([nid, rule]) => nid !== nodeId && ruleRefersToPerformerOf(rule, nodeId))
    .map(([nid]) => {
      const n = (schema.nodes || {})[nid];
      return n && n.label ? n.label : nid;
    });
  const body = el("div", { class: "form-grid" },
    el("div", { class: "card-hint" },
      "Dieser Login ist keinem Bearbeiter zugeordnet. Der Schritt wird an der Bearbeiterregel vorbei abgeschlossen (Aufsichtseingriff). Die Begr\u00FCndung wird im Audit-Verlauf festgehalten."),
    dependants.length
      ? el("div", { class: "warn-banner" },
          "\u26A0 Achtung: " + (dependants.length === 1 ? "Der Schritt " : "Die Schritte ")
          + dependants.map((n) => "\u201E" + n + "\u201C").join(", ")
          + (dependants.length === 1 ? " bestimmt" : " bestimmen")
          + " den Bearbeiter aus der Person, die diesen Schritt erledigt. Nach einem Aufsichtseingriff gibt es diese Person nicht \u2013 dort ist dann niemand zust\u00E4ndig, und auch das geht nur per Aufsicht weiter.")
      : null,
    el("label", { class: "field" }, "Begr\u00FCndung *", reason));
  openModal(`Aufsichtseingriff: ${label}`, body, async () => {
    const text = reason.value.trim();
    if (!text) { toast("err", "Bitte eine Begr\u00FCndung angeben"); return false; }
    return onConfirm(text);
  }, "Trotzdem abschlie\u00DFen");
}

/**
 * Anordnung einer Eingabemaske: Gruppen mit Ueberschrift, 1-3 Spalten.
 *
 * EINE Funktion fuer die Vorschau im Designer und die echte Aufgabenmaske,
 * damit beide gleich aussehen. Gruppen erscheinen in der Reihenfolge ihres
 * ersten Felds; Felder ohne Gruppe stehen ohne Ueberschrift an ihrer Stelle.
 * Am Smartphone ist die Maske immer einspaltig (CSS ``.mask-cols``).
 *
 * @param {{group?: string, node: HTMLElement}[]} items Felder in Maskenreihenfolge
 * @param {number} columns Spaltenzahl (1-3)
 * @returns {HTMLElement}
 */
function maskLayout(items, columns) {
  const wrap = el("div", { class: "mask-layout" });
  const cols = Math.min(3, Math.max(1, Number(columns) || 1));
  const sections = [];
  const byGroup = {};
  items.forEach((it) => {
    const key = (it.group || "").trim();
    if (!(key in byGroup)) { byGroup[key] = []; sections.push(key); }
    byGroup[key].push(it.node);
  });
  sections.forEach((key) => {
    if (key) wrap.appendChild(el("div", { class: "mask-group" }, key));
    wrap.appendChild(el("div", { class: "mask-cols", style: `grid-template-columns: repeat(${cols}, minmax(0, 1fr))` },
      ...byGroup[key]));
  });
  return wrap;
}

async function promptComplete(schema, instanceId, nodeId, label, agentId, onDone, dataValues) {
  // Bevorzugt die gestaltete Eingabemaske dieses Schritts; sonst generische
  // Felder fuer die Pflicht-Schreibvariablen.
  const form = (schema.forms || {})[nodeId];
  const values = dataValues || {};
  const inputs = {};
  const body = el("div", { class: "form-grid" });
  if (form) {
    if (form.title) body.appendChild(el("div", { class: "mask-title" }, form.title));
    body.appendChild(maskLayout(form.fields.map((f) => {
      const elem = schema.data_elements[f.element_id];
      const writable = f.mode === "WRITE" || f.mode === "READ_WRITE";
      const { control, read } = maskControl(elem, f.widget, f.options, values[f.element_id]);
      // Pruefregeln auch als HTML-Attribute (Tastatur, Browserhinweis).
      if (f.min_value != null) control.setAttribute("min", String(f.min_value));
      if (f.max_value != null) control.setAttribute("max", String(f.max_value));
      if (f.max_length != null) control.setAttribute("maxlength", String(f.max_length));
      const wrap = el("label", { class: "field" },
        f.label + (showsRequiredMark(f) && writable ? " *" : ""), control,
        f.help_text ? el("span", { class: "field-help" }, f.help_text) : null);
      if (!writable) {
        control.setAttribute("disabled", "disabled");
        // Nur-Lese-Zahlen wie ueberall anzeigen (Betrag „1.500,00“): ein
        // Zahlenfeld zeigte den Rohwert „1500“.
        const v = values[f.element_id];
        if (elem && isNumericType(elem.data_type) && typeof v === "number" && "value" in control) {
          control.setAttribute("type", "text");
          control.value = formatValue(elem, v);
        }
      } else inputs[f.element_id] = { read, elem, label: f.label, required: f.required !== false, field: f, wrap };
      return { group: f.group, node: wrap };
    }), form.columns || 1));
  } else {
    // Was der Schritt nur liest, steht als Nur-Lese-Zeile darueber: Wer
    // „Freigabe pruefen“ ohne Maske erledigte, sah den Betrag nicht, den er
    // freigab. Lesen-und-Schreiben erscheint unten als vorbelegtes Feld.
    const readOnly = readOnlyValues(schema, nodeId, values);
    if (readOnly) body.appendChild(readOnly);
    const writes = (schema.data_accesses || []).filter((a) => a.node_id === nodeId && (a.mode === "WRITE" || a.mode === "READ_WRITE"));
    writes.forEach((a) => {
      const elem = schema.data_elements[a.element_id];
      const { control, read } = maskControl(elem, fallbackWidget(elem), null, values[a.element_id]);
      const wrap = el("label", { class: "field" }, (elem ? elem.name : a.element_id) + ` (${elem ? typeName(elem.data_type) : "?"})`, control);
      inputs[a.element_id] = { read, elem, label: elem ? elem.name : a.element_id, required: true, wrap };
      body.appendChild(wrap);
    });
  }
  const doComplete = async () => {
    const data = {};
    // Ein leer gelassenes Pflichtfeld wird hier abgefangen statt vom Kern: der
    // fehlende Wert faellt sonst erst spaeter auf -- an einer XOR-Verzweigung
    // oder einer Folgeprozess-Bedingung, die ihn braucht -- und zwar als
    // Laufzeitfehler an einem *anderen* Schritt. Das Modell erklaert die
    // Pflicht bereits (FormField.required), also halten wir sie auch ein.
    const missing = [];
    const invalid = [];
    for (const [eid, { read, label: fieldLabel, required, field, wrap }] of Object.entries(inputs)) {
      const val = read();
      if (val === undefined) {
        // Pflichtfehler am Feld zeigen, nicht nur in der Meldung.
        markField(wrap, required ? "Pflichtfeld" : null);
        if (required) missing.push(fieldLabel);
        continue;
      }
      const problem = field ? fieldRuleProblem(field, val) : null;
      markField(wrap, problem);
      if (problem) { invalid.push(`${fieldLabel}: ${problem}`); continue; }
      // Kein Umwandeln hier: ``read()`` hat schon typisiert, was eindeutig ist
      // (``coerceTypedInput``); der Rest geht roh an den Kern und kommt als
      // D3-Meldung zurueck.
      data[eid] = val;
    }
    if (missing.length) {
      toast("err", "Bitte alle Pflichtfelder ausfüllen", missing);
      return false;
    }
    if (invalid.length) {
      toast("err", "Bitte die markierten Eingaben pr\u00FCfen", invalid);
      return false;
    }
    return submitCompletion(data, null);
  };
  // Sendet den Abschluss. Der Kern entscheidet, ob ein Aufsichtseingriff
  // vorliegt (Login ohne Agentenbindung an einem Schritt mit Bearbeiterregel) und
  // verlangt dann eine Begruendung -- der Client kennt diese Regel nicht, er
  // reagiert nur auf die Ablehnung und fragt nach (keine Korrektheitslogik hier).
  const submitCompletion = async (data, supervisionReason) => {
    try {
      const payload = { node_id: nodeId, data };
      if (agentId) payload.agent_id = agentId;
      if (supervisionReason) payload.supervision_reason = supervisionReason;
      await api.post(`/instances/${instanceId}/complete`, payload);
      toast("ok", "Schritt abgeschlossen");
      if (onDone) await onDone();
    } catch (err) {
      if (isSupervisionRequired(err)) { askSupervisionReason(label, (reason) => submitCompletion(data, reason), schema, nodeId); return false; }
      toastError(err); return false;
    }
  };
  // Ohne Maske und ohne Werte schloss ein einziger Klick den Schritt ab --
  // unumkehrbar und leicht versehentlich. Jetzt fragt ein kurzer Dialog nach;
  // der Fokus liegt auf „Abschliessen“, Enter bestaetigt.
  if (!form && !body.children.length) {
    body.appendChild(el("p", null,
      `\u201E${label}\u201C ist erledigt? Der Schritt hat keine Eingaben; abgeschlossen geht es mit dem n\u00E4chsten Schritt weiter.`));
  }
  openModal(`Abschlie\u00DFen: ${label}`, body, doComplete, "Abschlie\u00DFen");
}

/**
 * Nur-Lese-Anzeige der Werte, die ein Schritt ohne Maske liest.
 *
 * Ein Schritt mit gestalteter Maske zeigt Lesefelder selbst (deaktiviert); ohne
 * Maske gab es bisher nur die Schreibfelder -- eine Freigabe geschah blind.
 * Reine Anzeige, keine Logik: welche Werte ein Schritt liest, sagt die
 * Datenbindung des Modells.
 * @param {object} schema wirksames Schema der Instanz
 * @param {string} nodeId der abzuschliessende Schritt
 * @param {Record<string, *>} values aktuelle Vorgangsdaten
 * @returns {HTMLElement|null} Liste „Name: Wert“ oder null ohne Lesezugriffe
 */
function readOnlyValues(schema, nodeId, values) {
  const reads = (schema.data_accesses || []).filter((a) => a.node_id === nodeId && a.mode === "READ");
  if (!reads.length) return null;
  return el("dl", { class: "read-values" },
    ...reads.flatMap((a) => {
      const elem = (schema.data_elements || {})[a.element_id];
      return [
        el("dt", null, elem ? elem.name : a.element_id),
        el("dd", null, formatValue(elem, values[a.element_id])),
      ];
    }));
}

// --------------------------------------------------------------------------
// View: Monitoring
// --------------------------------------------------------------------------

/**
 * Dauer fuer die Engpass-Tabelle: „keine Zeitdaten“ statt eines Strichs, und
 * „< 1 s“ statt „0 s“ -- ein Strich sah aus wie ein fehlendes Feature.
 * @param {number|null|undefined} sec Sekunden
 * @returns {string}
 */
function fmtStepDuration(sec) {
  if (sec == null) return "keine Zeitdaten";
  if (sec < 1) return "< 1 s";
  return fmtDuration(sec);
}

/**
 * Panel „Prozesskarte (Soll/Ist)“ im Monitoring.
 *
 * Zeigt fuer ein gewaehltes Schema das Soll-Modell mit der beobachteten
 * Haeufigkeit und Dauer je Schritt (``renderGraph`` mit ``observed``) und
 * listet, was davon abweicht: Uebergaenge, die das Modell nicht vorsieht, und
 * Schritte, die im Modell fehlen (Ad-hoc). Die Rechnung liegt im Kern
 * (``GET /schemas/{id}/conformance``); der Client zeigt nur an. Die rohe
 * Directly-follows-Tabelle bleibt einklappbar erhalten.
 *
 * @param {object[]} instances die geladenen Instanzen (fuer die Schema-Auswahl)
 * @param {object|null} pmap entdeckte Prozesskarte (Directly-follows) oder null
 * @returns {Promise<HTMLElement>}
 */
async function conformancePanel(instances, pmap) {
  const withRuns = [...new Set(instances.map((i) => i.schema_id))];
  if (!state.conformanceSchema || !withRuns.includes(state.conformanceSchema)) {
    state.conformanceSchema = withRuns.includes(state.schemaId) ? state.schemaId : withRuns[0] || null;
  }
  const picker = el("select", { class: "conf-picker", onChange: (e) => { state.conformanceSchema = e.target.value; render(); } },
    ...withRuns.map((sid) => {
      const o = el("option", { value: sid }, schemaLabel(sid));
      if (sid === state.conformanceSchema) o.selected = true;
      return o;
    }));
  const body = el("div", { class: "panel-b" });
  const panel = el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Prozesskarte (Soll/Ist)"),
      el("span", { class: "sub" }, "Process Mining über dem Modell"), withRuns.length ? picker : null),
    body);
  if (!state.conformanceSchema) { body.appendChild(emptyState("Noch keine Abläufe. Starte eine Instanz und schließe Schritte ab.")); return panel; }

  let report = null, schema = null;
  try {
    [report, schema] = await Promise.all([
      api.get(`/schemas/${state.conformanceSchema}/conformance`),
      api.get(`/schemas/${state.conformanceSchema}`),
    ]);
  } catch (err) { body.appendChild(emptyState("Soll/Ist-Vergleich nicht verfügbar.")); return panel; }

  const observed = {};
  report.steps.forEach((st) => { observed[st.node_id] = st; });
  // fitOnShow: die Karte wird losgeloest gebaut und erst danach angehaengt --
  // ohne Einpassen lag das Modell beim ersten Oeffnen ausserhalb des Bildes.
  body.appendChild(renderGraph(schema, { observed, fitOnShow: true }));
  const never = report.steps.filter((st) => !st.completed);
  const facts = el("div", { class: "conf-facts" },
    el("span", null, `${countLabel(report.instances, "Instanz", "Instanzen")} ausgewertet`),
    el("span", null, never.length ? `${countLabel(never.length, "Schritt", "Schritte")} nie ausgeführt` : "jeder Schritt wurde ausgeführt"),
    el("span", { class: report.deviations.length ? "conf-warn" : "" },
      report.deviations.length ? `${countLabel(report.deviations.length, "Abweichung", "Abweichungen")} vom Modell` : "keine Abweichung vom Modell"));
  body.appendChild(facts);
  if (report.deviations.length) {
    const foreign = new Set(report.foreign_steps);
    const stepName = (id, label) => (label || id) + (foreign.has(id) ? " (nicht im Modell)" : "");
    body.appendChild(table(["Von", "Nach", "Häufigkeit"], report.deviations.map((d) => [
      stepName(d.source, d.source_label), stepName(d.target, d.target_label), String(d.frequency)])));
    body.appendChild(el("p", { class: "muted", style: "font-size:12px;margin-top:6px" },
      "Übergänge, die das Modell nicht erzeugen kann – meist Ad-hoc-Änderungen einzelner Instanzen oder Verläufe einer früheren Version."));
  }
  // Rohdaten (alle Schemata) bleiben erreichbar, aber eingeklappt.
  const edges = (pmap && pmap.edges) || [];
  if (edges.length) {
    const names = {};
    ((pmap && pmap.nodes) || []).forEach((n) => { names[n.node_id] = n.label || n.node_id; });
    body.appendChild(el("details", { class: "conf-raw" },
      el("summary", null, "Alle beobachteten Übergänge (Directly-follows, alle Prozesse)"),
      table(["Von", "Nach", "Häufigkeit"], edges.map((e) => [names[e.source] || e.source, names[e.target] || e.target, String(e.frequency)]))));
  }
  return panel;
}

/**
 * Filterstufen der Instanzliste im Monitoring (Wert = InstanceState, "all"
 * oder "UNSTAFFED" = laufend, aber ein offener Schritt hat niemanden).
 */
const INSTANCE_FILTERS = [
  { key: "all", label: "Alle" },
  { key: "RUNNING", label: "Laufend" },
  { key: "COMPLETED", label: "Abgeschlossen" },
  { key: "UNSTAFFED", label: "Niemand zust\u00E4ndig" },
  { key: "ABSENT_ONLY", label: "Nur Abwesende zust\u00E4ndig" },
];

/** Grund aus ``GET /monitoring/unstaffed`` als Satzteil. */
const UNSTAFFED_REASONS = {
  no_rule: "keine Bearbeiterzuordnung",
  nobody: "Regel findet aktuell niemanden",
  no_login: "kein Login",
  only_absent: "abwesend ohne Vertretung",
};

/**
 * Grund einer Zeile aus ``GET /monitoring/unstaffed`` als Text – bei
 * „kein Login“ und „abwesend ohne Vertretung“ mit den betroffenen Personen
 * (Namen aus dem Personenverzeichnis), damit die Aufsicht weiss, wen es angeht.
 *
 * @param {{reason: string, agent_ids?: string[]}} u die Zeile
 * @returns {string} z. B. „kein Login: Petra Pruef“
 */
function unstaffedReasonText(u) {
  const base = UNSTAFFED_REASONS[u.reason] || u.reason;
  const names = (u.agent_ids || []).map(agentNameOf);
  return names.length ? `${base}: ${names.join(", ")}` : base;
}

/**
 * Teilt den Bericht in die zwei Kategorien des Monitorings.
 *
 * „Niemand zust\u00E4ndig“ (``no_rule``, ``nobody``, ``no_login``): der Vorgang
 * steht still, bis jemand zugeordnet wird oder eine Aufsicht abschliesst.
 * „Nur Abwesende zust\u00E4ndig“ (``only_absent``): schwaecher – die Aufgabe
 * bleibt bei der Person und laeuft weiter, sobald sie zurueck ist.
 *
 * @param {object[]} unstaffed Zeilen aus ``GET /monitoring/unstaffed``
 * @returns {{stalled: object[], absentOnly: object[]}}
 */
function splitUnstaffed(unstaffed) {
  const rows = unstaffed || [];
  return {
    stalled: rows.filter((u) => u.reason !== "only_absent"),
    absentOnly: rows.filter((u) => u.reason === "only_absent"),
  };
}

/**
 * Zaehlkacheln des Monitorings: aus dem KPI-Bericht, nicht aus der Liste.
 *
 * Die Anleitung sagt zu: Kennzahlen ueber **alle** Vorgaenge, einzeln nur die,
 * an denen ein Bearbeiter beteiligt ist. Die Liste (``GET /instances``) ist fuer
 * einen solchen Login gefiltert und enthaelt dazu Test-Instanzen, die nicht ins
 * Monitoring zaehlen; der Bericht (``GET /monitoring/kpis``) zaehlt alle echten
 * Vorgaenge. Frueher kamen „Instanzen gesamt“ aus der Liste, die
 * Durchlaufzeit und die Engpaesse aus dem Bericht -- widerspruechlich ohne
 * Hinweis. Die Einschraenkung kennt der Client nicht; er erkennt sie daran,
 * dass die Liste weniger echte Vorgaenge zeigt als der Bericht zaehlt.
 *
 * @param {object[]} instances geladene Vorgaenge der Liste
 * @param {?{total_instances: number, running: number, completed: number}} report
 *   KPI-Bericht; ``null``, wenn er nicht geladen werden konnte
 * @param {boolean} [listLoaded=true] ``false``, wenn das Laden der Liste
 *   scheiterte -- eine leere oder kurze Liste ist dann keine Einschraenkung,
 *   und der Hinweis darauf waere irrefuehrend
 * @returns {{total: number, running: number, done: number, listIsPartial: boolean}}
 *   ``listIsPartial``: Die Liste (und alles, was aus ihr gezaehlt wird) zeigt
 *   nur einen Teil der Vorgaenge. Ohne Bericht zaehlt die Liste ohne
 *   Test-Instanzen, ``listIsPartial`` ist dann ``false`` (nicht feststellbar).
 */
function monitorCounts(instances, report, listLoaded = true) {
  const real = instances.filter((i) => !i.is_test);
  if (!report) {
    return {
      total: real.length,
      running: real.filter((i) => i.state === "RUNNING").length,
      done: real.filter((i) => i.state === "COMPLETED").length,
      listIsPartial: false,
    };
  }
  return {
    total: report.total_instances,
    running: report.running,
    done: report.completed,
    listIsPartial: listLoaded && real.length < report.total_instances,
  };
}

async function viewMonitor() {
  const content = byId("content");
  clear(content);
  let instances = [];
  let listLoaded = false;
  try {
    const ids = await api.get("/instances");
    instances = await Promise.all(ids.map((id) => api.get(`/instances/${id}`)));
    listLoaded = true;
    await ensureSchemaNames(instances.map((i) => i.schema_id));
  } catch (err) { toastError(err); }

  // KPI-Report + Prozesskarte aus dem Audit-Log (Schritt 15)
  let report = null;
  let pmap = null;
  try { report = await api.get("/monitoring/kpis"); } catch (e) { /* ignore */ }
  try { pmap = await api.get("/monitoring/process-map"); } catch (e) { /* ignore */ }

  // Offene Schritte, die niemand bearbeiten darf -- der Vorgang steht
  // still, zeigte aber „ueberfaellig 0, eskaliert 0“. Die Rechnung liegt im Kern
  // (GET /monitoring/unstaffed); hier nur Kachel, Liste und Filter.
  let unstaffed = [];
  try { unstaffed = await api.get("/monitoring/unstaffed"); } catch (e) { /* best-effort */ }
  // Benennende Werte je Vorgang; fehlen sie, bleibt die ID.
  let titles = {};
  try { titles = await api.get("/instance-titles"); } catch (e) { /* best-effort */ }
  const { stalled, absentOnly } = splitUnstaffed(unstaffed);
  const instancesById = Object.fromEntries(instances.map((i) => [i.id, i]));
  const unstaffedIds = new Set(stalled.map((u) => u.instance_id));
  const absentOnlyIds = new Set(absentOnly.map((u) => u.instance_id));

  // Z4: \u00DCberf\u00E4llig-Zusammenfassung \u00FCber alle
  // laufenden Vorg\u00E4nge \u2013 best-effort aus den vorhandenen Task-Endpunkten.
  // Dieselbe Sammlung tr\u00E4gt die Eskalations-Sicht (T3/E9 Stufe C): welche
  // Aufgaben haben bereits Stufen gefeuert.
  let overdueTasks = 0;
  let escalatedTasks = [];
  try {
    const lists = await Promise.all(instances
      .filter((i) => i.state === "RUNNING")
      .map((i) => api.get(`/instances/${i.id}/tasks`).catch(() => [])));
    const allTasks = lists.flat();
    overdueTasks = allTasks.filter((t) => t.time_criticality === "OVERDUE").length;
    escalatedTasks = allTasks.filter((t) => (t.escalated_stage || 0) > 0);
  } catch (e) { /* best-effort: Kacheln zeigen dann 0 */ }

  const counts = monitorCounts(instances, report, listLoaded);
  const kpis = el("div", { class: "kpis" },
    kpi("Instanzen gesamt", counts.total),
    kpi("Laufend", counts.running),
    kpi("Abgeschlossen", counts.done),
    kpi("\u00DCberf\u00E4llige Aufgaben", overdueTasks),
    kpi("Eskalierte Aufgaben", escalatedTasks.length),
    kpi("Niemand zust\u00E4ndig", unstaffedIds.size),
    kpi("Nur Abwesende zust\u00E4ndig", absentOnlyIds.size),
    kpi("\u00D8 Durchlaufzeit", report ? fmtDuration(report.avg_cycle_seconds) : "\u2013"));
  content.appendChild(kpis);
  if (counts.listIsPartial) {
    content.appendChild(el("div", { class: "muted monitor-scope-note" },
      "Anzahl, Durchlaufzeit und Engp\u00E4sse z\u00E4hlen alle Vorg\u00E4nge. "
      + "\u00DCberf\u00E4llige und eskalierte Aufgaben, die Zust\u00E4ndigkeits-Kacheln und die Liste "
      + "zeigen nur Vorg\u00E4nge, an denen du beteiligt bist."));
  }

  // Eskalations-Sicht (Stufe C): jede Aufgabe mit
  // gefeuerten Stufen, klickbar zur Instanz. Nur sichtbar, wenn es etwas zu
  // zeigen gibt \u2013 ein Betrieb ohne Eskalationen bekommt kein leeres Panel.
  if (escalatedTasks.length) {
    const escRows = escalatedTasks.map((t) => [
      instanceNameCell(t.instance_id, t.instance_started_at, t.context),
      t.label || t.node_id,
      `Stufe ${t.escalated_stage}`,
      criticalityBadge(t) || "\u2013",
      t.claimed_by || "\u2013"]);
    content.appendChild(el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, "Eskalationen"),
        el("span", { class: "sub" }, "Aufgaben mit gefeuerten Eskalationsstufen")),
      el("div", { class: "panel-b" },
        table(["Instanz", "Schritt", "Eskalation", "Kritikalit\u00E4t", "Inhaber"], escRows,
          (i) => ({ class: "clickable", onClick: () => openInstanceFromMonitor(escalatedTasks[i].instance_id) })))));
  }

  // „Niemand zustaendig“ / „Nur Abwesende zustaendig“: nur sichtbar, wenn
  // es etwas zu zeigen gibt.
  const unstaffedPanel = (rows, title, sub) => {
    const cells = rows.map((u) => [
      instanceNameCell(u.instance_id, (instancesById[u.instance_id] || {}).started_at, titles[u.instance_id]),
      schemaLabel(u.schema_id, u.schema_version),
      u.label || u.node_id,
      unstaffedReasonText(u)]);
    return el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, title), el("span", { class: "sub" }, sub)),
      el("div", { class: "panel-b" },
        table(["Instanz", "Schema", "Schritt", "Grund"], cells,
          (i) => ({ class: "clickable", onClick: () => openInstanceFromMonitor(rows[i].instance_id) }))));
  };
  if (stalled.length) {
    content.appendChild(unstaffedPanel(stalled, "Niemand zust\u00E4ndig",
      "Diese Vorg\u00E4nge stehen still, bis jemand zugeordnet wird, einen Login bekommt oder per Aufsicht abschlie\u00DFt"));
  }
  if (absentOnly.length) {
    content.appendChild(unstaffedPanel(absentOnly, "Nur Abwesende zust\u00E4ndig",
      "Die Aufgabe bleibt bei der Person und l\u00E4uft weiter, sobald sie zur\u00FCck ist \u2013 oder eine Vertretung eintragen"));
  }

  // Filter der Instanzliste: die Liste hiess
  // „Aktive Instanzen“, zeigte aber auch abgeschlossene. Rein clientseitig,
  // transient (state.monitorFilter), Standard "alle".
  const filter = INSTANCE_FILTERS.some((f) => f.key === state.monitorFilter) ? state.monitorFilter : "all";
  const matches = (i, key) => key === "all"
    || (key === "UNSTAFFED" ? unstaffedIds.has(i.id)
      : key === "ABSENT_ONLY" ? absentOnlyIds.has(i.id) : i.state === key);
  const shown = instances.filter((i) => matches(i, filter));
  const filterBar = el("div", { class: "seg-filter", role: "group", "aria-label": "Instanzen filtern" },
    ...INSTANCE_FILTERS.map((f) => {
      const n = instances.filter((i) => matches(i, f.key)).length;
      return el("button", {
        class: "seg-btn" + (f.key === filter ? " active" : ""),
        "aria-pressed": f.key === filter ? "true" : "false",
        onClick: () => { state.monitorFilter = f.key; render(); },
      }, `${f.label} (${n})`);
    }));
  const rows = shown.map((i) => {
    const total = Object.keys(i.node_states || {}).length || 1;
    const completed = Object.values(i.node_states || {}).filter((s) => s === "COMPLETED" || s === "SKIPPED").length;
    const pct = Math.round((completed / total) * 100);
    const idCell = instanceNameCell(i.id, i.started_at, titles[i.id]);
    return { i, cells: [idCell, schemaLabel(i.schema_id, i.schema_version), statePillFor(i.state), `${pct}%`] };
  });

  // Leere Liste: ein Hinweis statt einer Tabelle mit einer Leerzeile.
  const tbl = rows.length
    ? table(["Instanz", "Schema", "Status", "Fortschritt"], rows.map((r) => r.cells),
        (i) => ({ class: rows[i].i.id === state.instanceId ? "clickable selected" : "clickable",
          onClick: () => openInstanceFromMonitor(rows[i].i.id) }))
    : emptyState(instances.length
        ? "Keine Instanzen in diesem Filter."
        : "Keine Instanzen. Starte eine in der Ausf\u00FChrungs-Sicht.");

  content.appendChild(el("div", { class: "panel", "data-tour": "monitor.instances" },
    el("div", { class: "panel-h" }, el("h2", null, "Instanzen"), el("span", { class: "sub" }, "Klick \u00F6ffnet Detail"), filterBar),
    el("div", { class: "panel-b" }, tbl)));

  // Detail der ausgewaehlten Instanz inkl. Live-Prozesslandkarte -- direkt unter
  // der Liste der aktiven Instanzen, damit der Bezug sofort sichtbar ist.
  // Die ausgewaehlte Instanz frisch laden: ``state.instance`` stammt sonst aus
  // einer frueheren Sicht und zeigte beim Oeffnen ihren Startzustand, bis man
  // die Zeile anklickte. Die Revisions-Abfrage rendert bei neuem
  // Fortschritt ohnehin neu -- damit bleibt das Detail aktuell. Ist sie nicht
  // mehr lesbar (geloescht, fremd), faellt die Auswahl weg.
  if (state.instanceId) {
    try { await loadInstance(state.instanceId); }
    catch (e) {
      if (!dropUnreadableInstance(e)) { state.instanceId = null; state.instance = null; state.worklist = null; }
    }
  }
  if (state.instance) {
    const detail = el("div");
    // Schema der Instanz laden, damit der Graph passt
    if (state.instance.schema_id !== state.schemaId) {
      try { state.schema = await api.get(`/schemas/${state.instance.schema_id}`); } catch (e) { /* ignore */ }
    }
    await renderInstanceDetail(detail, true);
    content.appendChild(detail);
  }

  // Engpass-Analyse: je Schritt Liegezeit (bereit -> uebernommen), Bearbeitung
  // (uebernommen -> erledigt) und Gesamtdauer (bereit -> erledigt). Die
  // Gesamtdauer gibt es auch, wenn ohne Uebernahme erledigt wurde -- vorher stand
  // dann ueberall „–“. Sortiert nach Gesamtdauer: der Engpass steht oben.
  const stats = ((report && report.activity_stats) || []).slice()
    .sort((a, b) => (b.avg_total_seconds ?? -1) - (a.avg_total_seconds ?? -1));
  const statRows = stats.map((s) => [s.label || s.node_id, String(s.completed),
    fmtStepDuration(s.avg_total_seconds), fmtStepDuration(s.avg_wait_seconds), fmtStepDuration(s.avg_duration_seconds)]);
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Engp\u00E4sse \u2013 Aktivit\u00E4ten"),
      el("span", { class: "sub" }, "\u00D8 Dauer von \u201Ebereit\u201C bis \u201Eerledigt\u201C \u2013 l\u00E4ngste zuerst")),
    el("div", { class: "panel-b" }, statRows.length
      ? el("div", null,
          table(["Aktivit\u00E4t", "Abschl\u00FCsse", "\u00D8 gesamt", "\u00D8 Liegezeit", "\u00D8 Bearbeitung"], statRows),
          el("p", { class: "muted", style: "font-size:12px;margin-top:8px" },
            "Liegezeit und Bearbeitung gibt es nur für Aufgaben, die vor dem Erledigen übernommen wurden. „keine Zeitdaten“: Abschlüsse aus der Zeit vor dieser Messung."))
      : emptyState("Noch keine abgeschlossenen Aktivit\u00E4ten erfasst."))));

  // Prozesskarte Soll/Ist: das Beobachtete ueber dem Soll-Modell (dieselbe
  // Zeichnung wie ueberall), dazu die Abweichungen. Frueher nur eine Tabelle
  // „von / nach / Haeufigkeit“ ohne Bezug zum Modell.
  content.appendChild(await conformancePanel(instances, pmap));

  // Inzidente externer Aufgaben. Sichtbar fuer alle
  // Monitoring-Leser; "Erneut versuchen" (Aufloesen + Wiedereinreihen) ist nur
  // fuer Bearbeiter/Administratoren freigeschaltet (tasks:complete).
  let incidents = [];
  try { incidents = await api.get("/v1/incidents?unresolved_only=true"); }
  catch (e) { /* ignore: integration runtime may be disabled */ }
  const canResolve = hasRole("operator", "admin");
  const incBody = el("div", { class: "panel-b" });
  if (!incidents.length) {
    incBody.appendChild(el("div", { class: "ok-banner" }, "\u2713 Keine offenen Inzidente externer Aufgaben."));
  } else {
    const incRows = incidents.map((inc) => {
      const action = canResolve
        ? el("button", { class: "btn small green", onClick: () => resolveIncident(inc) }, "Erneut versuchen")
        : el("span", { class: "muted" }, "\u2013");
      return [inc.node_id, inc.message, fmtTimestamp(new Date(inc.created_at * 1000).toISOString()), action];
    });
    incBody.appendChild(table(["Schritt", "Fehler", "Zeit", ""], incRows));
  }
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Inzidente (externe Aufgaben)"),
      el("span", { class: "sub" }, "Topic-Fehler \u00B7 Aufl\u00F6sen reiht die Aufgabe erneut ein")),
    incBody));
}

// --------------------------------------------------------------------------
// View: Administration (nur Rolle Administrator)
// --------------------------------------------------------------------------

// Buendelt alle rein administrativen Betriebstaetigkeiten in einer eigenen,
// nur fuer Administratoren sichtbaren Sicht (die Nav-Sichtbarkeit steuert
// VIEW_ROLES.admin + applyRoleNav). Bisher lagen diese Panels am unteren Ende
// des Monitorings; sie sind hier zentral zusammengefasst:
//   - Datensicherung: Zustand der automatischen Sicherung ansehen und bei
//     Bedarf sofort eine Sicherung anstossen (GET/POST /admin/backups[/run-now]).
//   - Wartung: gesamtes System auf Null zuruecksetzen bzw. Beispieldaten laden
//     (POST /admin/reset) -- destruktiv, daher mit Bestaetigung.
// Die Sicht ist rein an der Betriebsschicht orientiert und traegt keine
// Korrektheitslogik -- jede Aktion laeuft ueber die bestehenden Kern-Endpunkte.
async function viewAdmin() {
  const content = byId("content");
  clear(content);

  // Doppelte Absicherung: Non-Admins sehen die Nav-Kachel gar nicht erst
  // (applyRoleNav), aber falls die Sicht doch aktiv wird (z. B. persistierte
  // View nach Rollenwechsel), zeigen wir nur einen Hinweis statt Aktionen.
  if (!hasRole("admin")) {
    content.appendChild(el("div", { class: "panel" },
      el("div", { class: "panel-b" },
        emptyState("Dieser Bereich ist Administratoren vorbehalten."))));
    return;
  }

  // Benutzer (nur Passwort-Modus): die Logins verwalten -- auflisten,
  // Passwort zuruecksetzen, loeschen. Rollen aendert die Oberflaeche bewusst
  // nicht; neue Logins entstehen an der Person in der Ressourcensicht.
  if (state.passwordLogin) {
    const usersBody = el("div", { class: "panel-b" });
    content.appendChild(el("div", { class: "panel", "data-panel": "admin.users" },
      el("div", { class: "panel-h" },
        el("h2", null, "Benutzer"),
        el("span", { class: "sub" }, "Logins \u00B7 Passwort zur\u00FCcksetzen \u00B7 l\u00F6schen")),
      usersBody));
    loadUsersPanel(usersBody);
  }

  // Sicherungen (nur Ansicht): zeigt den Zustand der automatischen
  // Datensicherung und erlaubt, sofort eine Sicherung anzustossen. Die
  // Oberflaeche fuehrt selbst KEIN pg_dump aus -- sie setzt nur einen
  // Ausloese-Marker; der Sicherungsdienst fuehrt sie kurz darauf aus.
  const backupsBody = el("div", { class: "panel-b" });
  content.appendChild(el("div", { class: "panel", "data-tour": "admin.backups" },
    el("div", { class: "panel-h" },
      el("h2", null, "Sicherungen"),
      el("span", { class: "sub" }, "Datensicherung \u00B7 Zustand und Sofort-Sicherung")),
    backupsBody));
  loadBackupsPanel(backupsBody);

  // E-Mail-Ausgang (nur Ansicht): der Zustand der durablen Mail-Outbox der
  // modellierten Benachrichtigungen (Regelgruppe N) -- was ausstehend, in
  // Wiederholung, zugestellt oder als Dead-Letter gescheitert ist. Ein manueller
  // Versand stoesst faellige Eintraege erneut an (POST /admin/mail-outbox/dispatch).
  const mailBody = el("div", { class: "panel-b" });
  content.appendChild(el("div", { class: "panel", "data-tour": "admin.mail" },
    el("div", { class: "panel-h" },
      el("h2", null, "E-Mail-Ausgang"),
      el("span", { class: "sub" }, "Benachrichtigungen · Zustand und erneuter Versand")),
    mailBody));
  loadMailOutboxPanel(mailBody);

  // Wartung: destruktiv, daher unter den Sicherungen. Nur Administratoren
  // duerfen zuruecksetzen oder Beispieldaten laden (POST /admin/reset).
  content.appendChild(el("div", { class: "panel", "data-tour": "admin.maintenance" },
    el("div", { class: "panel-h" },
      el("h2", null, "Wartung"),
      el("span", { class: "sub" }, "Daten zur\u00FCcksetzen \u00B7 Beispiel laden")),
    el("div", { class: "panel-b" },
      el("p", { class: "muted" },
        "Setzt das gesamte System zur\u00FCck. Die Beispieldaten zeigen alle Funktionen anhand zweier Prozesse, einer Organisation und drei Vorg\u00E4ngen (zwei laufend, einer abgeschlossen). Das Order-to-Cash-Beispiel ist der gro\u00DFe Datensatz: sechs Prozesse (Haupt-, Teil- und Folgeprozesse) vom Angebot bis zum Mahnwesen, mit neun Auftragsvorg\u00E4ngen an unterschiedlichen Stellen (samt Teil- und Folgeprozessen 21 Vorg\u00E4nge). Dieser Vorgang l\u00F6scht alle vorhandenen Daten unwiderruflich."),
      el("div", { style: "display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;" },
        el("button", { class: "btn primary", onClick: () => confirmReset("demo") }, "Beispieldaten laden"),
        el("button", { class: "btn", onClick: () => confirmReset("o2c") }, "Order-to-Cash-Beispiel laden"),
        el("button", { class: "btn danger", onClick: () => confirmReset("wipe") }, "Auf Null zur\u00FCcksetzen")))));
}

// Groessenangabe menschenlesbar (Bytes -> KB/MB/GB ...).
function fmtBytes(n) {
  if (n == null) return "\u2013";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, x = n;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  return fmtNumber(x, i ? 1 : 0) + " " + units[i];
}

// Fuellt den Sicherungen-Bereich asynchron (der Monitoring-Render bleibt sync).
async function loadBackupsPanel(body) {
  clear(body);
  body.appendChild(el("p", { class: "muted" }, "Wird geladen \u2026"));
  try {
    const status = await api.get("/admin/backups");
    clear(body);
    body.appendChild(renderBackupsBody(status, body));
  } catch (err) {
    clear(body);
    const d = describeError(err);
    body.appendChild(el("p", { class: "muted" }, d.title));
  }
}

// Baut den Inhalt des Sicherungen-Panels aus dem Statusobjekt.
function renderBackupsBody(status, body) {
  const frag = document.createDocumentFragment();

  if (!status || !status.available) {
    frag.appendChild(el("p", { class: "muted" },
      "F\u00FCr diese Installation ist keine Sicherungs\u00FCbersicht eingerichtet. " +
      "Im geb\u00FCndelten Stack sichert der Dienst automatisch t\u00E4glich; Details im Betriebs-Backup-Leitfaden."));
    return frag;
  }

  const info = el("div", { class: "muted", style: "margin-bottom:10px;" });
  info.appendChild(el("div", null, "Letzte erfolgreiche Sicherung: " +
    (status.last_success ? fmtTimestamp(status.last_success) : "\u2013 (noch keine)")));
  if (status.last_verify_success) {
    info.appendChild(el("div", null, "Letzter erfolgreicher Selbsttest: " + fmtTimestamp(status.last_verify_success)));
  }
  frag.appendChild(info);

  const backups = status.backups || [];
  if (backups.length) {
    const rows = backups.map((b) => [
      b.file,
      b.created_at ? fmtTimestamp(b.created_at) : "\u2013",
      b.app_version || "\u2013",
      fmtBytes(b.size_bytes),
      b.encrypted ? "ja" : "nein",
    ]);
    frag.appendChild(table(["Datei", "Zeitpunkt", "Version", "Gr\u00F6\u00DFe", "Verschl\u00FCsselt"], rows));
  } else {
    frag.appendChild(el("p", { class: "muted" }, "Noch keine Sicherungen vorhanden."));
  }

  frag.appendChild(el("div", { style: "display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;" },
    el("button", { class: "btn", onClick: () => triggerBackupNow(body) }, "Jetzt sichern"),
    el("button", { class: "btn small", onClick: () => loadBackupsPanel(body) }, "Aktualisieren")));

  // Wiederherstellung ist bewusst KEINE GUI-Funktion: ein Restore verwirft die
  // laufende Datenbank und laeuft mit erhoehten Rechten am Dump-Volume, auf das
  // die API nach der Sicherheitsregel "kein Web-/API-Zugriff auf das
  // Backup-Verzeichnis" bewusst keinen Zugriff hat. Er erfolgt daher als
  // gefuehrter Ops-Ablauf (restore.sh). Hier nur ein Hinweis mit Verweis.
  const restoreNote = el("p", { class: "muted", style: "margin-top:14px;" },
    "ℹ️ Eine Wiederherstellung erfolgt aus Sicherheitsgründen nicht über die " +
    "Oberfläche, sondern als geführter Betriebsablauf (restore.sh). Anleitung: ");
  restoreNote.appendChild(el("a",
    { href: docUrl("Betriebs-Backup-Leitfaden.md"), target: "_blank", rel: "noopener" },
    "Betriebs-Backup-Leitfaden"));
  restoreNote.appendChild(document.createTextNode("."));
  frag.appendChild(restoreNote);
  return frag;
}

// Fordert eine Sofort-Sicherung an (Marker); der Dienst fuehrt sie kurz darauf aus.
async function triggerBackupNow(body) {
  try {
    await api.post("/admin/backups/run-now");
    toast("ok", "Sicherung angefordert",
      ["Der Sicherungsdienst f\u00FChrt sie in K\u00FCrze aus. \u201EAktualisieren\u201C zeigt sie danach an."]);
    setTimeout(() => loadBackupsPanel(body), 5000);
  } catch (err) {
    toastError(err);
  }
}

// Epoch-Sekunden (Float) menschenlesbar; die Mail-Outbox stempelt so (analog outbox.py).
function fmtEpoch(sec) {
  if (!sec) return "–";
  try { return new Date(sec * 1000).toLocaleString("de-DE"); } catch (_e) { return "–"; }
}

const MAIL_STATE_LABELS = {
  PENDING: "ausstehend", FAILED: "in Wiederholung", SENT: "zugestellt", DEAD: "gescheitert",
  DROPPED: "verworfen \u2013 kein Mailserver",
};

// Fuellt den E-Mail-Ausgang-Bereich asynchron (GET /admin/mail-outbox).
async function loadMailOutboxPanel(body) {
  clear(body);
  body.appendChild(el("p", { class: "muted" }, "Wird geladen …"));
  try {
    const status = await api.get("/admin/mail-outbox");
    clear(body);
    body.appendChild(renderMailOutboxBody(status, body));
  } catch (err) {
    clear(body);
    const d = describeError(err);
    body.appendChild(el("p", { class: "muted" }, d.title));
  }
}

// Baut den Inhalt des E-Mail-Ausgang-Panels aus dem Statusobjekt. Zeigt bewusst
// nur Metadaten (Empfaengeranzahl, Zustand, Betreff) -- keine Adressliste, keinen
// Text (Datensparsamkeit, DSGVO-freundlich).
function renderMailOutboxBody(status, body) {
  const frag = document.createDocumentFragment();

  const info = el("div", { class: "muted", style: "margin-bottom:10px;" });
  if (!status.configured) {
    info.appendChild(el("div", null,
      "Kein SMTP-Server konfiguriert – Benachrichtigungen werden modelliert und " +
      "protokolliert, aber nicht versendet. Den Mailserver in deploy/.env eintragen (PROCWORKS_SMTP_HOST, PROCWORKS_MAIL_FROM u. a.; siehe README, Abschnitt \u201EEinstellungen\u201C)."));
  }
  info.appendChild(el("div", null,
    `Gesamt ${status.total} · ausstehend ${status.pending} · in Wiederholung ` +
    `${status.failed} · zugestellt ${status.sent} · gescheitert ${status.dead}`
    + ` · verworfen (kein Mailserver) ${status.dropped || 0}`));
  frag.appendChild(info);

  const entries = status.entries || [];
  if (entries.length) {
    const rows = entries.map((e) => [
      MAIL_STATE_LABELS[e.state] || e.state,
      e.node_label || e.node_id,
      e.subject || "–",
      String(e.recipient_count),
      `${e.attempts}/${e.max_attempts}`,
      fmtEpoch(e.created_at),
      e.last_error ? el("span", { class: "pill pill-red", title: e.last_error }, "Fehler") : "–",
    ]);
    frag.appendChild(table(
      ["Zustand", "Schritt", "Betreff", "Empf.", "Versuche", "Angelegt", "Letzter Fehler"], rows));
  } else {
    frag.appendChild(el("p", { class: "muted" }, "Noch keine Benachrichtigung in der Warteschlange."));
  }

  frag.appendChild(el("div", { style: "display:flex; gap:10px; margin-top:12px; flex-wrap:wrap;" },
    el("button", { class: "btn", onClick: () => dispatchMailOutbox(body) }, "Fällige jetzt senden"),
    el("button", { class: "btn small", onClick: () => loadMailOutboxPanel(body) }, "Aktualisieren")));
  return frag;
}

// Stoesst einen manuellen Versand faelliger Eintraege an (POST …/dispatch) und
// zeigt das Ergebnis. Nuetzlich nach einer SMTP-Stoerung.
async function dispatchMailOutbox(body) {
  try {
    const status = await api.post("/admin/mail-outbox/dispatch");
    clear(body);
    body.appendChild(renderMailOutboxBody(status, body));
    toast("ok", "Versand angestoßen", [`Zugestellt: ${status.sent} · gescheitert: ${status.dead}`
      + ` · verworfen (kein Mailserver): ${status.dropped || 0}`]);
  } catch (err) { toastError(err); }
}

async function openInstanceFromMonitor(id) {
  try {
    await loadInstance(id);
    if (state.instance.schema_id !== state.schemaId) {
      state.schemaId = state.instance.schema_id;
      await refreshSchema();
      renderSchemaPicker();
    }
    render();
  } catch (err) { toastError(err); }
}

// Administrator-Wartung: System zur\u00FCcksetzen bzw. einen Beispieldatensatz
// laden. ``kind`` ist "demo" (schlanker Kosmos), "o2c" (der grosse
// Order-to-Cash-Datensatz) oder "wipe" (leeres System). Alle drei laufen ueber
// denselben Endpunkt POST /admin/reset, der immer zuerst loescht.
const RESET_KINDS = {
  demo: {
    title: "Beispieldaten laden",
    confirm: "Beispieldaten laden",
    done: "Beispieldaten geladen",
    msg: "Alle vorhandenen Daten werden gel\u00F6scht und durch die Beispieldaten ersetzt. Im Login-Betrieb entstehen dabei Beispiel-Anmeldungen (u. a. eine Modelliererin) mit einem zuf\u00E4lligen Passwort, das danach einmal angezeigt wird. M\u00F6chten Sie fortfahren?",
    body: { load_demo: true },
  },
  o2c: {
    title: "Order-to-Cash-Beispiel laden",
    confirm: "Order-to-Cash laden",
    done: "Order-to-Cash-Beispiel geladen",
    msg: "Alle vorhandenen Daten werden gel\u00F6scht und durch den Order-to-Cash-Datensatz ersetzt: sechs Prozesse, eine eigene Organisation und neun Vorg\u00E4nge an unterschiedlichen Stellen. Im Login-Betrieb entstehen dabei Beispiel-Anmeldungen mit einem zuf\u00E4lligen Passwort, das danach einmal angezeigt wird. M\u00F6chten Sie fortfahren?",
    body: { load_o2c: true },
  },
  wipe: {
    title: "Auf Null zur\u00FCcksetzen",
    confirm: "Endg\u00FCltig l\u00F6schen",
    done: "System auf Null zur\u00FCckgesetzt",
    msg: "Alle Schemata, Instanzen und Organisationsmodelle werden gel\u00F6scht. Im Login-Betrieb werden zus\u00E4tzlich alle Nutzer au\u00DFer Ihnen und dem Administrator-Konto entfernt. Dieser Schritt kann nicht r\u00FCckg\u00E4ngig gemacht werden.",
    body: {},
  },
};

function confirmReset(kind) {
  const spec = RESET_KINDS[kind] || RESET_KINDS.wipe;
  openModal(
    spec.title,
    el("p", { class: "muted" }, spec.msg),
    async () => {
      const password = await runReset(kind);
      // Der Passwort-Dialog ersetzt diesen hier; ``true`` wuerde ihn beim
      // Schliessen gleich wieder entfernen (openModal teilt sich einen Container).
      if (password) { showExamplePassword(password); return false; }
      return true;
    },
    spec.confirm, { danger: true });
}

/**
 * Fuehrt die Wartung aus (POST /admin/reset) und laedt die Oberflaeche neu.
 * @param {string} kind "demo", "o2c" oder "wipe" (siehe RESET_KINDS)
 * @returns {Promise<string|null>} das Passwort neu angelegter Beispiel-
 *   Anmeldungen -- der Aufrufer zeigt es an -- sonst ``null``
 */
async function runReset(kind) {
  const spec = RESET_KINDS[kind] || RESET_KINDS.wipe;
  try {
    const res = await api.post("/admin/reset", spec.body);
    const lines = [
      `Schemata: ${res.schemas}`,
      `Instanzen: ${res.instances}`,
      `Organisationsmodelle: ${res.org_models}`,
    ];
    if (state.passwordLogin) lines.push(`Nutzerkonten: ${res.users}`);
    toast("ok", spec.done, lines);
    // Auswahl zur\u00FCcksetzen, da bisherige Schemata/Instanzen evtl. weg sind.
    state.instance = null;
    state.schema = null;
    state.schemaId = null;
    await boot();
    return res.example_password || null;
  } catch (err) { toastError(err); return null; }
}

/**
 * Zeichnet die Benutzerverwaltung (Administration → Benutzer) in ``body``.
 *
 * Eine Zeile je Login: Login, Name, Rollen, zugeordnete Person (aus dem
 * Personenverzeichnis, sonst die id) und Zustand („muss Passwort aendern“).
 * Aktionen: „Passwort zuruecksetzen“ (neues Initialpasswort einmal angezeigt,
 * Sitzungen enden) und „Loeschen“ (mit Bestaetigung). Der eigene Login ist
 * nicht loeschbar – der Knopf ist gesperrt und nennt den Grund; der Kern
 * lehnt es zusaetzlich ab, ebenso das Loeschen des letzten Administrators.
 *
 * @param {HTMLElement} body Panel-Koerper; wird bei jedem Aufruf neu gefuellt
 * @returns {Promise<void>}
 */
async function loadUsersPanel(body) {
  let users;
  try { users = await api.get("/users"); }
  catch (err) {
    clear(body);
    body.appendChild(emptyState(describeError(err).title));
    return;
  }
  clear(body);
  const me = state.principal && state.principal.subject;
  const reload = () => loadUsersPanel(body);
  const rows = (users || []).slice().sort((a, b) => a.login.localeCompare(b.login)).map((u) => {
    const roles = (u.roles || []).map((r) => ROLE_LABELS[r] || r).join(", ") || "\u2013";
    const person = u.agent_id
      ? ((state.agentDirectory[u.agent_id] && state.agentDirectory[u.agent_id].name) || u.agent_id)
      : el("span", { class: "muted" }, "\u2013");
    const status = u.must_change
      ? el("span", { class: "pill pill-amber", title: "Initialpasswort noch nicht ge\u00E4ndert" }, "muss Passwort \u00E4ndern")
      : el("span", { class: "pill pill-green" }, "aktiv");
    const own = u.login === me;
    const actions = el("div", { style: "display:flex; gap:6px; justify-content:flex-end; flex-wrap:wrap" },
      el("button", { class: "btn small", onClick: () => confirmUserPasswordReset(u, reload) },
        "Passwort zur\u00FCcksetzen"),
      el("button", { class: "btn small danger", disabled: own,
        title: own ? "Den eigenen Login kann man nicht l\u00F6schen \u2013 sonst w\u00E4re man ausgesperrt." : null,
        onClick: () => confirmUserDelete(u, reload) }, "L\u00F6schen"));
    return [u.login, u.display_name || "\u2013", roles, person, status, actions];
  });
  body.appendChild(rows.length
    ? table(["Login", "Name", "Rollen", "Person", "Zustand", ""], rows)
    : emptyState("Keine Logins."));
}

/**
 * Rueckfrage und Zuruecksetzen des Passworts eines Logins.
 *
 * Nach Erfolg ersetzt der Zugangsdaten-Dialog diesen (Rueckgabe ``false``);
 * das neue Initialpasswort steht nur dort. Bestehende Sitzungen des Logins
 * beendet der Kern.
 *
 * @param {{login: string}} user der Login
 * @param {() => void} onDone laedt die Liste neu
 */
function confirmUserPasswordReset(user, onDone) {
  openModal(`Passwort zur\u00FCcksetzen: ${user.login}`,
    el("p", { class: "muted" },
      "Erzeugt ein neues Initialpasswort, das nur einmal angezeigt wird. Bestehende Anmeldungen dieses Logins enden; beim n\u00E4chsten Anmelden vergibt die Person ein eigenes Passwort."),
    async () => {
      try {
        const res = await api.post(`/users/${encodeURIComponent(user.login)}/reset-password`);
        showLoginCredentials(res, "Neues Initialpasswort");
        onDone();
        return false;
      } catch (err) { toastError(err); return false; }
    }, "Zur\u00FCcksetzen", { danger: true });
}

/**
 * Bestaetigung und Loeschen eines Logins.
 *
 * Die Person im Organisationsmodell bleibt erhalten; nur die Anmeldung
 * verschwindet, und ihre Sitzungen enden. Lehnt der Kern ab (eigener Login,
 * letzter Administrator), steht sein Grund in der Fehlermeldung.
 *
 * @param {{login: string}} user der Login
 * @param {() => void} onDone laedt die Liste neu
 */
function confirmUserDelete(user, onDone) {
  openModal(`Login l\u00F6schen: ${user.login}`,
    el("p", { class: "muted" },
      "Der Login wird entfernt, laufende Anmeldungen enden sofort. Die Person im Organisationsmodell, ihre Aufgaben und der Verlauf bleiben erhalten."),
    async () => {
      try {
        await api.del(`/users/${encodeURIComponent(user.login)}`);
        toast("ok", "Login gel\u00F6scht", [user.login]);
        onDone();
      } catch (err) { toastError(err); return false; }
    }, "L\u00F6schen", { danger: true });
}

/**
 * Zeigt das Passwort der gerade angelegten Beispiel-Anmeldungen -- genau
 * einmal, in einem Dialog statt einer verschwindenden Meldung.
 *
 * Bis 1.27.2 trugen diese Anmeldungen das auf der Website veroeffentlichte
 * Demo-Passwort; auf einer Kundeninstallation war damit u. a. eine
 * Modellierer-Anmeldung fuer jeden offen. Der Server vergibt jetzt ein
 * zufaelliges Passwort und nennt es nur in der Antwort auf das Laden.
 *
 * Ergebnisdialog (``cancel: false``): nur „Verstanden“ und ein Kopierknopf;
 * der Text verweist auf Administration → Benutzer zum Aufraeumen.
 * @param {string} password das Passwort aller Beispiel-Anmeldungen
 */
function showExamplePassword(password) {
  openModal("Beispiel-Anmeldungen angelegt",
    el("div", null,
      el("p", null, "Die Beispieldaten bringen Anmeldungen mit, z. B. ",
        el("code", null, "mara.modell"), " oder ", el("code", null, "erika.sander"),
        ". Ihr gemeinsames Passwort wird nur jetzt angezeigt:"),
      copyField("Passwort der Beispiel-Anmeldungen", password),
      el("p", { class: "muted", style: "font-size:13px" },
        "Vor dem echten Einsatz die Beispiel-Anmeldungen unter Administration \u2192 Benutzer l\u00F6schen oder ihnen neue Passw\u00F6rter geben \u2013 oder das System auf Null zur\u00FCcksetzen.")),
    async () => true, "Verstanden", { cancel: false });
}

/** Deutsche Namen der Vorgangszustaende (statt RUNNING/COMPLETED roh). */
const INSTANCE_STATE_LABELS = { RUNNING: "l\u00E4uft", COMPLETED: "abgeschlossen" };

function statePillFor(s) {
  return el("span", { class: "pill " + (s === "COMPLETED" ? "pill-green" : "pill-blue"), title: s },
    INSTANCE_STATE_LABELS[s] || s);
}

// Inzident eines externen Tasks aufloesen (Aufgabe wird erneut eingereiht).
async function resolveIncident(inc) {
  try {
    await api.post(`/v1/incidents/${inc.id}/resolve`);
    render();
    toast("ok", "Inzident aufgel\u00F6st", ["Aufgabe erneut eingereiht."]);
  } catch (err) { toastError(err); }
}

// Audit-/Monitoring-Hilfen (Schritt 15)
const EVENT_LABELS = {
  INSTANCE_CREATED: "Instanz erstellt",
  ACTIVITY_STARTED: "Aktivit\u00E4t gestartet",
  ACTIVITY_COMPLETED: "Aktivit\u00E4t abgeschlossen",
  ACTIVITY_SUPERVISED: "Aufsichtseingriff",
  BRANCH_DECIDED: "Zweig entschieden",
  ADHOC_INSERTED: "Ad-hoc eingef\u00FCgt",
  ADHOC_DELETED: "Ad-hoc gel\u00F6scht",
  ADHOC_RENAMED: "Ad-hoc umbenannt",
  INSTANCE_MIGRATED: "Instanz migriert",
  INSTANCE_DATA_SET: "Daten ge\u00E4ndert",
  INSTANCE_COMPLETED: "Instanz abgeschlossen",
  ACTIVITY_CLAIMED: "Aufgabe \u00FCbernommen",
  ACTIVITY_RETURNED: "Aufgabe zur\u00FCckgelegt",
  ACTIVITY_SUSPENDED: "Aufgabe angehalten",
  ACTIVITY_RESUMED: "Weiterarbeit aufgenommen",
  ACTIVITY_FAILED: "Problem gemeldet",
  ACTIVITY_RESET: "Wiederanlauf",
  TASK_ESCALATED: "Eskaliert",
  MAIL_SENT: "E-Mail versendet",
  MAIL_FAILED: "E-Mail gescheitert",
  TIME_ANCHOR: "Zeitbezug gesetzt",
};

function eventLabel(t) { return EVENT_LABELS[t] || t; }

/**
 * Wer hat das Ereignis ausgeloest? Reihenfolge: gebundener Bearbeiter, dann der
 * Login (`detail.actor`, gesetzt wenn kein Agent dahintersteht), sonst
 * "System" -- und das nur fuer Ereignisse, die tatsaechlich die Maschine
 * ausloest. Fruehere Fassung zeigte "System" auch fuer manuelle Abschluesse.
 * @param {{agent_id?: string|null, detail?: Record<string,string>}} ev Audit-Ereignis
 * @returns {string}
 */
function auditActorLabel(ev) {
  if (ev.agent_id) return agentNameOf(ev.agent_id);
  const actor = ev.detail && ev.detail.actor;
  if (actor) return actor === "anonymous" ? "unbekannt (offener Zugang)" : actor;
  return "System";
}

/**
 * Detailspalte eines Audit-Ereignisses: Schrittname; bei einem Aufsichtseingriff
 * zusaetzlich die Begruendung.
 * @param {{label?: string|null, node_id?: string|null, detail?: Record<string,string>}} ev Audit-Ereignis
 * @returns {string}
 */
function auditDetailText(ev) {
  let base = ev.label || ev.node_id || "\u2013";
  const detail = ev.detail || {};
  // Datenaenderung: Element mit altem und neuem Wert. Beide reisen als
  // JSON im Detail; ``null`` heisst „war noch nicht gesetzt".
  if (ev.event_type === "INSTANCE_DATA_SET" && "new" in detail) {
    const show = (raw) => {
      let v;
      try { v = JSON.parse(raw); } catch (e) { v = raw; }
      if (v === null || v === undefined) return "leer";
      if (v === true) return "Ja";
      if (v === false) return "Nein";
      return String(v);
    };
    base = `${base}: ${show(detail.old)} \u2192 ${show(detail.new)}`;
  }
  // Ad-hoc: ``label`` ist der Schritt davor bzw. der alte Name, das
  // Detail traegt den neuen Schritt bzw. den neuen Namen.
  if (ev.event_type === "ADHOC_INSERTED" && detail.label) {
    base = `\u201E${detail.label}\u201C nach ${base}`;
  } else if (ev.event_type === "ADHOC_RENAMED" && detail.label) {
    base = `${base} \u2192 \u201E${detail.label}\u201C`;
  }
  const reason = detail.reason;
  return reason ? `${base} \u2013 Begr\u00FCndung: ${reason}` : base;
}

function fmtTimestamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("de-DE");
}

function fmtDuration(sec) {
  if (sec == null) return "\u2013";
  // „0.0 s" sah aus wie eine kaputte Kachel, war aber die ehrliche Auskunft
  // ueber Vorgaenge, die in derselben Sekunde entstanden und endeten.
  // Die Demo-Daten haben einen Zeitverlauf; bleibt der Wert dennoch unter einer Sekunde, sagt die Kachel
  // das jetzt, statt eine Null zu zeigen.
  if (sec < 1) return "< 1 s";
  if (sec < 60) return fmtNumber(sec, 1) + " s";
  if (sec < 3600) return fmtNumber(sec / 60, 1) + " min";
  return fmtNumber(sec / 3600, 1) + " h";
}

// --------------------------------------------------------------------------
// View: Meine Aufgaben (Bearbeiter-Aufgabenliste)
// --------------------------------------------------------------------------

/**
 * Anzeigename eines Agenten, unabhaengig vom oben gewaehlten Prozess.
 *
 * Erst das Organisationsmodell des gewaehlten Schemas (dort ist der Name
 * sicher der des Vorgangs), dann das modelluebergreifende Verzeichnis
 * (`state.agentDirectory`, gefuellt aus ``GET /directory/agents``), zuletzt die
 * ID. Ohne den zweiten Schritt zeigte "Meine Aufgaben" interne IDs, sobald oben
 * ein Prozess mit einer anderen Organisation gewaehlt war -- die Aufgabenliste
 * reicht ueber alle Prozesse, die Namensaufloesung darf es also auch.
 *
 * @param {string} id Agenten-ID
 * @returns {string} Name oder, wenn unbekannt, die ID
 */
function agentNameOf(id) {
  const org = state.schema && state.schema.org_model;
  const a = org && org.agents ? org.agents[id] : null;
  if (a) return a.name;
  const known = state.agentDirectory[id];
  return known ? known.name : id;
}

/**
 * Laedt das modelluebergreifende Personenverzeichnis in `state.agentDirectory`.
 *
 * Best effort: schlaegt der Abruf fehl (fehlende Leserechte, alter Server),
 * bleibt das Verzeichnis leer und die Anzeige faellt auf das bisherige
 * Verhalten zurueck. Wird vor den Sichten aufgerufen, die Personen ueber
 * Prozessgrenzen hinweg zeigen.
 *
 * @returns {Promise<void>}
 */
async function loadAgentDirectory() {
  try {
    const entries = await api.get("/directory/agents");
    const dir = {};
    entries.forEach((a) => { dir[a.agent_id] = a; });
    state.agentDirectory = dir;
  } catch (e) { /* best effort -- ohne Verzeichnis bleibt es bei den IDs */ }
}

// Track the agent whose task list is on screen plus the keys of the tasks last
// shown there, so a task that arrives while the list is open can be announced
// exactly once with a self-dismissing popup.
let taskAlert = { agentId: null, keys: new Set(), primed: false };

function taskKey(t) { return `${t.instance_id}:${t.node_id}`; }

// Announce tasks that appeared since the last render of THIS agent's list while
// the tasks view stayed open. The initial load (no baseline yet) and agent
// switches only prime the baseline, so a popup pops only for genuine arrivals.
function announceNewTasks(agentId, tasks) {
  const keys = new Set(tasks.map(taskKey));
  if (taskAlert.agentId === agentId && taskAlert.primed) {
    const fresh = tasks.filter((t) => !taskAlert.keys.has(taskKey(t)));
    if (fresh.length === 1) {
      toast("info", "Neue Aufgabe eingetroffen", [fresh[0].label || fresh[0].node_id]);
    } else if (fresh.length > 1) {
      toast("info", `${fresh.length} neue Aufgaben eingetroffen`, fresh.slice(0, 5).map((t) => t.label || t.node_id));
    }
  }
  taskAlert = { agentId, keys, primed: true };
}

async function viewTasks() {
  const content = byId("content");
  clear(content);
  // Die persoenliche Aufgabenliste haengt bewusst NICHT am oben gewaehlten
  // Prozess: sie reicht ueber alle Prozesse (/me/tasks, /agents/{id}/tasks).
  // Vorher brach sie genau daran -- war oben ein Prozess ohne eigene Agenten
  // gewaehlt, sah eine Sachbearbeiterin statt ihrer Aufgaben einen Hinweis fuer
  // Modellierer. Das Personenverzeichnis kommt
  // deshalb modelluebergreifend aus /directory/agents.
  await loadAgentDirectory();
  const agents = Object.values(state.agentDirectory).map((a) => ({ id: a.agent_id, name: a.name }));

  // A bound principal (token login) is tied to one agent: no picker, the
  // worklist comes from /me/tasks. In open dev mode we keep the agent picker.
  // A *personal* login without an agent binding (password, company account)
  // gets the picker only to look at someone's list (supervision): it may not
  // act in that person's name -- the core answers 403 since 2026-09-24 --, so
  // the rows offer just the supervision completion and the recovery actions
  // that work without an owner (see supervisionTaskActions).
  const bound = state.principal && state.principal.agent_id;
  const supervise = !bound && !mayActForOthers();
  if (!bound && !agents.length) {
    content.appendChild(emptyState(hasRole("modeler", "admin")
      ? "Keine Agenten in den Organisationsmodellen. Lege zuerst Agenten in der Ressourcensicht an."
      : "Dieser Login ist keinem Bearbeiter zugeordnet \u2013 wende dich an die Administration."));
    return;
  }
  let agentId;
  let picker;
  if (bound) {
    agentId = state.principal.agent_id;
    const who = state.principal.display_name || agentNameOf(agentId);
    picker = el("div", { class: "panel" },
      el("div", { class: "panel-h" }, el("h2", null, "Angemeldet"), el("span", { class: "sub" }, "Aufgaben f\u00FCr dich, inkl. Vertretung")),
      el("div", { class: "panel-b" }, el("div", { class: "ok-banner" }, "\u2713 Angemeldet als " + who)));
  } else {
    agentId = localStorage.getItem("agentId");
    if (!agentId || !agents.some((a) => a.id === agentId)) agentId = agents[0].id;
    const sel = el("select", null, ...agents.map((a) => el("option", { value: a.id }, a.name)));
    sel.value = agentId;
    sel.addEventListener("change", () => { localStorage.setItem("agentId", sel.value); render(); });
    picker = supervise
      ? el("div", { class: "panel" },
          el("div", { class: "panel-h" }, el("h2", null, "Aufsicht"), el("span", { class: "sub" }, "Aufgaben einer Person ansehen, inkl. Vertretung")),
          el("div", { class: "panel-b" },
            el("label", { class: "field" }, "Aufgaben ansehen von", sel),
            el("div", { class: "warn-banner", style: "margin-top:8px" },
              "Dein Login ist keinem Bearbeiter zugeordnet. Du kannst deshalb nicht im Namen dieser Person arbeiten. " +
              "Einen Schritt kannst du als Aufsichtseingriff mit Begr\u00FCndung abschlie\u00DFen; er wird unter deinem Login protokolliert.")))
      : el("div", { class: "panel" },
          el("div", { class: "panel-h" }, el("h2", null, "Bearbeiter"), el("span", { class: "sub" }, "Aufgaben f\u00FCr eine Person, inkl. Vertretung")),
          el("div", { class: "panel-b" }, el("label", { class: "field" }, "Angemeldet als", sel)));
  }
  content.appendChild(picker);

  let tasks = [];
  try {
    tasks = await api.get(bound ? "/me/tasks" : `/agents/${agentId}/tasks`);
    await ensureSchemaNames(tasks.map((t) => t.schema_id));
  }
  catch (err) { toastError(err); }

  // Announce tasks that arrived while this list was open (self-dismissing).
  announceNewTasks(agentId, tasks);

  // Z4: clientseitiger Kritikalit\u00E4ts-Filter.
  // Rein additiv \u00FCber die vorhandenen API-Felder; Wahl wird gemerkt.
  const taskFilter = localStorage.getItem("taskFilter") || "all";
  const visible = taskFilter === "critical"
    ? tasks.filter((t) => t.time_criticality === "AT_RISK" || t.time_criticality === "OVERDUE")
    : taskFilter === "overdue"
      ? tasks.filter((t) => t.time_criticality === "OVERDUE")
      : tasks;
  const hiddenCount = tasks.length - visible.length;
  const filterSel = el("select", { class: "task-filter" },
    el("option", { value: "all" }, "Alle Aufgaben"),
    el("option", { value: "critical" }, "Nur kritische (wird knapp + \u00FCberf\u00E4llig)"),
    el("option", { value: "overdue" }, "Nur \u00FCberf\u00E4llige"));
  filterSel.value = taskFilter;
  filterSel.addEventListener("change", () => {
    localStorage.setItem("taskFilter", filterSel.value);
    render();
  });

  const body = el("div", { class: "panel-b" });
  if (!tasks.length) {
    body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Keine offenen Aufgaben f\u00FCr " + agentNameOf(agentId) + "."));
  } else if (!visible.length) {
    body.appendChild(el("div", { class: "ok-banner" },
      `\u2713 Keine ${taskFilter === "overdue" ? "\u00FCberf\u00E4lligen" : "kritischen"} Aufgaben \u2013 ${hiddenCount} weitere unter \u201EAlle Aufgaben\u201C.`));
  } else {
    // E1 (Zustandsmaschine): Eine unübernommene Aufgabe bietet „Übernehmen“
    // an (sie verschwindet dann aus den Listen aller anderen), eine selbst
    // übernommene „Zurücklegen“. Fremd Übernommenes taucht hier gar nicht
    // erst auf – das filtert der Kern (Withdrawn-Sicht). „Erledigen“ geht
    // weiterhin auch ohne Übernahme (Ein-Klick-Fluss).
    const rows = visible.map((t) => {
      const elig = (t.eligible_agents || []).map(agentNameOf).join(", ");
      const mine = t.claimed_by === agentId;
      const status = el("div", { class: "row", style: "gap:4px" },
        mine
          ? el("span", { class: "pill pill-amber" }, "übernommen")
          : el("span", { class: "pill pill-gray" }, "angeboten"),
        // E2: Detailzustand (Pause/Scheitern) direkt am Eintrag.
        t.detail === "SUSPENDED" ? el("span", { class: "pill pill-blue" }, "angehalten") : null,
        t.detail === "FAILED"
          ? el("span", { class: "pill pill-red", title: t.detail_reason || "" }, "gescheitert")
          : null,
        // T3/E9: eine gefeuerte Eskalationsstufe ist am Eintrag ablesbar.
        t.escalated_stage > 0
          ? el("span", { class: "pill pill-red" }, `eskaliert (${t.escalated_stage})`)
          : null);
      // E2-Aktionen je Detailzustand: gescheitert → nur Wiederanlauf;
      // angehalten → Weiterarbeiten/Zurücklegen; sonst wie gehabt, für den
      // Inhaber ergänzt um Anhalten und Problem melden.
      let actions;
      if (supervise) {
        actions = supervisionTaskActions(t);
      } else if (t.detail === "FAILED") {
        actions = el("div", { class: "row", style: "gap:6px" },
          el("button", { class: "btn small", onClick: () => resetTask(t, agentId) }, "Wiederanlauf"));
      } else if (t.detail === "SUSPENDED") {
        actions = el("div", { class: "row", style: "gap:6px" },
          el("button", { class: "btn small green", onClick: () => resumeTask(t, agentId) }, "Weiterarbeiten"),
          el("button", { class: "btn small ghost", onClick: () => returnTask(t, agentId) }, "Zurücklegen"));
      } else {
        actions = el("div", { class: "row", style: "gap:6px" },
          el("button", { class: "btn small green", onClick: () => completeTask(t, agentId) }, "Erledigen"),
          mine
            ? el("button", { class: "btn small ghost", onClick: () => returnTask(t, agentId) }, "Zurücklegen")
            : el("button", { class: "btn small ghost", onClick: () => claimTask(t, agentId) }, "Übernehmen"),
          mine
            ? el("button", { class: "btn small ghost", title: "Arbeit pausieren (Frist läuft weiter)",
                onClick: () => suspendTask(t, agentId) }, "Anhalten")
            : null,
          mine
            ? el("button", { class: "btn small ghost", title: "Als gescheitert melden",
                onClick: () => failTask(t, agentId) }, "Problem")
            : null);
      }
      // Zwei gleiche Aufgaben verschiedener Vorgaenge waren nicht zu
      // unterscheiden -- darunter jetzt die benennenden Werte.
      // Ohne benennende Werte (meist beim ersten Schritt) unterscheidet die
      // Startzeit des Vorgangs.
      const taskCell = el("div", { title: `Kennung: ${t.instance_id}` }, t.label || t.node_id,
        el("div", { class: "task-context" }, instanceName(t.instance_started_at, t.context)),
        deputyNote(t, agentId));
      return [taskCell, schemaLabel(t.schema_id, t.schema_version), dueCell(t), elig, status, actions];
    });
    body.appendChild(table(["Aufgabe", "Prozess", "Fällig", "Berechtigte", "Status", ""], rows));
    if (hiddenCount > 0) {
      body.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-top:6px" },
        `${countLabel(hiddenCount, "Aufgabe", "Aufgaben")} durch den Filter ausgeblendet.`));
    }
  }
  content.appendChild(el("div", { class: "panel", "data-tour": "tasks.list" },
    el("div", { class: "panel-h" }, el("h2", null, "Offene Aufgaben"),
      el("span", { class: "sub" },
        hiddenCount > 0 ? `${visible.length} von ${tasks.length} Eintr\u00E4gen` : countLabel(tasks.length, "Eintrag", "Eintr\u00E4ge")),
      filterSel),
    body));

  content.appendChild(await absencePanel(agentId));
}

/**
 * „in Vertretung für <Name>“ an einer Aufgabe der Arbeitsliste.
 *
 * Eine Vertretung sah die Aufgabe einer abwesenden Kollegin ohne Hinweis,
 * warum. Der Kern nennt je Vertretung die vertretene Person
 * (``OpenTask.deputy_of``); der Name kommt aus dem Personenverzeichnis, weil
 * die Liste ueber alle Prozesse reicht.
 * @param {{deputy_of?: Object<string, string>}} task Aufgabe
 * @param {string} agentId Person, deren Liste gezeigt wird
 * @returns {HTMLElement|null} Vermerk, oder ``null``, wenn die Person die
 *   Aufgabe selbst traegt
 */
function deputyNote(task, agentId) {
  const forId = task.deputy_of && task.deputy_of[agentId];
  if (!forId) return null;
  const known = state.agentDirectory && state.agentDirectory[forId];
  return el("div", { class: "task-deputy" }, `in Vertretung f\u00FCr ${known ? known.name : forId}`);
}

/**
 * Hinweis zum Vertretungs-Status im Abwesenheits-Panel.
 *
 * @param {string|null} person Name der Person, deren Liste gezeigt wird –
 *   ``null``, wenn es die eigene ist (dann „du/deine“).
 * @param {string|null} deputy Name der Vertretung, ``null`` ohne Vertretung
 * @returns {HTMLElement} gruener bzw. gelber Hinweis. Ohne Vertretung nennt er,
 *   wer sie hinterlegen kann: Modellierer und Administratoren in der
 *   Ressourcensicht; ein Bearbeiter sieht diese Sicht nicht und wird an die
 *   Administration verwiesen.
 */
function absenceDeputyBanner(person, deputy) {
  const howToSet = hasRole("modeler", "admin")
    ? "Vertretung in der Ressourcensicht setzen."
    : "Bitte die Vertretung bei der Administration hinterlegen lassen.";
  if (deputy) {
    return el("div", { class: "ok-banner" }, person
      ? `\u2713 Vertretung hinterlegt: ${deputy} erh\u00E4lt die Aufgaben von ${person} w\u00E4hrend der Abwesenheit (parallel).`
      : `\u2713 Vertretung hinterlegt: ${deputy} erh\u00E4lt deine Aufgaben w\u00E4hrend deiner Abwesenheit (parallel zu dir).`);
  }
  return el("div", { class: "warn-banner" }, person
    ? `\u26A0 F\u00FCr ${person} ist keine Vertretung hinterlegt. W\u00E4hrend der Abwesenheit bleiben die Aufgaben bei ${person} \u2013 niemand \u00FCbernimmt sie; das Monitoring zeigt solche Vorg\u00E4nge unter \u201ENur Abwesende zust\u00E4ndig\u201C. ${howToSet}`
    : `\u26A0 Keine Vertretung hinterlegt. W\u00E4hrend deiner Abwesenheit bleiben deine Aufgaben dir zugewiesen \u2013 die Instanz steht nicht still, aber niemand \u00FCbernimmt f\u00FCr dich. ${howToSet}`);
}

// Panel \u201EAbwesenheit / Vertretung" in \u201EMeine Aufgaben": eine gut sichtbare
// Selbstbedienung, um fuer einen Zeitraum abwesend zu sein. Waehrend der
// Abwesenheit erhaelt der eingetragene Vertreter die Aufgaben PARALLEL zum
// Agenten (der Agent wird nie entfernt -- ohne Vertretung bleibt die Aufgabe
// also beim Agenten und die Instanz steht nie still). Die eigentliche Aufloesung
// passiert im Kern; dieser Client ruft nur die Endpunkte /agents/{id}/absences.
async function absencePanel(agentId) {
  // Wie die Aufgabenliste selbst: die Person wird modelluebergreifend gesucht
  // (Verzeichnis), nicht im gerade gewaehlten Schema -- sonst haette ein
  // Bearbeiter je nach Prozessauswahl "keine Vertretung hinterlegt" gelesen,
  // obwohl eine eingetragen ist.
  const org = (state.schema && state.schema.org_model) || { agents: {} };
  const agent = (org.agents || {})[agentId] || state.agentDirectory[agentId] || {};
  const deputyId = agent.deputy_id || null;

  // Vertretungs-Status: ohne Vertreter ein deutlicher Hinweis, dass die
  // Aufgaben in der Abwesenheit beim Agenten selbst verbleiben. Sieht eine
  // Aufsicht die Liste einer anderen Person, nennt der Text diese Person.
  const own = !!(state.principal && state.principal.agent_id === agentId);
  const deputyLine = absenceDeputyBanner(own ? null : (agent.name || agentNameOf(agentId)),
    deputyId ? agentNameOf(deputyId) : null);

  // Eingabezeile: Zeitraum (von/bis, ganze Tage) plus optionale Notiz.
  const fromInp = el("input", { type: "date" });
  const toInp = el("input", { type: "date" });
  const noteInp = el("input", { type: "text", placeholder: "Notiz (optional), z. B. Urlaub" });
  const addBtn = el("button", { class: "btn small primary", onClick: submit }, "Abwesenheit eintragen");

  async function submit() {
    if (!fromInp.value || !toInp.value) { toast("err", "Bitte Start- und Enddatum w\u00E4hlen"); return; }
    if (toInp.value < fromInp.value) { toast("err", "Das Enddatum darf nicht vor dem Startdatum liegen"); return; }
    try {
      // Ganze Tage: Beginn 00:00, Ende 23:59:59 UTC (inklusiver Zeitraum).
      await api.post(`/agents/${agentId}/absences`, {
        start_at: `${fromInp.value}T00:00:00Z`,
        end_at: `${toInp.value}T23:59:59Z`,
        note: noteInp.value || "",
      });
      toast("ok", "Abwesenheit eingetragen");
      render();
    } catch (err) { toastError(err); }
  }

  let absences = [];
  try { absences = await api.get(`/agents/${agentId}/absences`); }
  catch (err) { toastError(err); }

  const now = new Date();
  const fmt = fmtDate;
  const listBody = el("div", { class: "panel-b" });
  if (!absences.length) {
    listBody.appendChild(el("div", { class: "muted", style: "font-size:13px" }, "Keine Abwesenheiten eingetragen."));
  } else {
    const rows = absences.map((a) => {
      const active = new Date(a.start_at) <= now && now <= new Date(a.end_at);
      const status = active ? el("span", { class: "pill pill-amber" }, "aktiv") : el("span", { class: "pill pill-gray" }, "geplant");
      const del = el("button", { class: "btn small danger", onClick: async () => {
        try { await api.del(`/agents/${agentId}/absences/${a.id}`); toast("ok", "Abwesenheit entfernt"); render(); }
        catch (err) { toastError(err); }
      } }, "Entfernen");
      return [`${fmt(a.start_at)} \u2013 ${fmt(a.end_at)}`, a.note || "\u2013", status, del];
    });
    listBody.appendChild(table(["Zeitraum", "Notiz", "Status", ""], rows));
  }

  return el("div", { class: "panel", "data-tour": "tasks.absence" },
    el("div", { class: "panel-h" },
      el("h2", null, "Abwesenheit / Vertretung"),
      el("span", { class: "sub" }, "Vertreter erh\u00E4lt Aufgaben w\u00E4hrend der Abwesenheit")),
    el("div", { class: "panel-b" },
      deputyLine,
      el("div", { class: "form-row", style: "display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end;margin-top:10px" },
        el("label", { class: "field" }, "Von", fromInp),
        el("label", { class: "field" }, "Bis", toInp),
        el("label", { class: "field", style: "flex:1;min-width:180px" }, "Notiz", noteInp),
        addBtn)),
    listBody);
}

// Darf dieser Login im Namen einer anderen Person handeln (agent_id nennen)?
// Spiegelt nur die Regel des Kerns (api._may_act_for_others), damit die
// Oberfläche keine Knöpfe anbietet, die mit 403 enden -- entschieden wird im
// Kern. Ja nur im offenen Entwicklungsmodus, im Token-Modus (Integrationen)
// und für Integrations-Identitäten. Persönliche Logins (Passwort,
// Firmenkonto) handeln nur als sie selbst.
function mayActForOthers() {
  if (state.authMode === "open" || state.authMode === "token") return true;
  const roles = (state.principal && state.principal.roles) || [];
  return roles.includes("integration");
}

// Aktionen einer Aufgabe in der Aufsichtssicht von „Meine Aufgaben" (Login ohne
// Bearbeiterzuordnung). Keine davon nennt eine Person: Abschließen läuft als
// Aufsichtseingriff (der Kern verlangt die Begründung, promptComplete fragt
// danach), Zurücklegen und Wiederanlauf einer fremd übernommenen Aufgabe sind
// die Eingriffe, die der Kern der Aufsicht ohnehin erlaubt.
//   t: Eintrag der Aufgabenliste (OpenTask) -- Rückgabe: Knopfzeile (Element).
function supervisionTaskActions(t) {
  if (t.detail === "FAILED") {
    return el("div", { class: "row", style: "gap:6px" },
      el("button", { class: "btn small", onClick: () => resetTask(t, null) }, "Wiederanlauf"));
  }
  if (t.detail === "SUSPENDED") {
    return el("div", { class: "row", style: "gap:6px" },
      el("button", { class: "btn small ghost", onClick: () => returnTask(t, null) }, "Zurücklegen"));
  }
  return el("div", { class: "row", style: "gap:6px" },
    el("button", { class: "btn small",
      title: "Der Abschluss wird als Aufsichtseingriff mit Begründung unter deinem Login protokolliert",
      onClick: () => completeTask(t, null) }, "Als Aufsicht abschließen"),
    t.claimed_by
      ? el("button", { class: "btn small ghost", onClick: () => returnTask(t, null) }, "Zurücklegen")
      : null);
}

/**
 * Setzt eine Arbeitslisten-Aktion (E1/E2) fuer einen offenen Schritt ab.
 *
 * Das gemeinsame Geruest von Uebernehmen, Zuruecklegen, Anhalten,
 * Weiterarbeiten, Problem melden und Wiederanlauf: ``POST
 * /instances/{id}{action}`` mit ``node_id`` und ``agent_id``, danach
 * Erfolgsmeldung und Neuzeichnen. Ob die Aktion erlaubt ist (Exklusivitaet W1,
 * Berechtigung W2, Detailzustands-Automat), entscheidet allein der Kern; eine Ablehnung
 * (409/403) erscheint nur als Meldung.
 *
 * @param {object} task Offene Aufgabe mit ``instance_id`` und ``node_id``.
 * @param {string} agentId Handelnde Person (``agent_id`` der Anfrage).
 * @param {string} action Pfadendung samt Schraegstrich, z. B. ``"/claim"``.
 * @param {string} okTitle Titel der Erfolgsmeldung.
 * @param {string[]} [okLines] Optionale Detailzeilen der Erfolgsmeldung.
 * @param {object} [extra] Weitere Felder des Anfragekoerpers (etwa ``reason``).
 * @returns {Promise<boolean>} ``true`` nach Erfolg, ``false`` nach einem
 *   Fehler -- passend fuer den Bestaetigen-Rueckruf von ``openModal``.
 */
async function postTaskAction(task, agentId, action, okTitle, okLines, extra) {
  try {
    await api.post(`/instances/${task.instance_id}${action}`,
      { node_id: task.node_id, agent_id: agentId, ...extra });
    toast("ok", okTitle, okLines);
    render();
    return true;
  } catch (err) {
    toastError(err);
    return false;
  }
}

// E1 (Zustandsmaschine): Aufgabe übernehmen bzw. zurücklegen. Geteilte
// Funktionen für jede Aufrufstelle (Aufgabenliste heute, künftige Sichten) –
// der Kern erzwingt Exklusivität (W1) und Berechtigung (W2) und antwortet bei
// Konflikten mit 409, das hier nur angezeigt wird (keine Client-Logik).
async function claimTask(task, agentId) {
  await postTaskAction(task, agentId, "/claim", "Aufgabe übernommen",
    ["Sie verschwindet aus den Listen der anderen, bis sie erledigt oder zurückgelegt ist."]);
}

async function returnTask(task, agentId) {
  await postTaskAction(task, agentId, "/return", "Aufgabe zurückgelegt",
    ["Alle Berechtigten sehen sie wieder in ihrer Liste."]);
}

// --- Detailzustände (E2): Anhalten / Weiterarbeiten / Problem / Wiederanlauf.
// Geteilte Funktionen; die Regeln (nur Inhaber, gescheitert wartet auf
// Wiederanlauf, Detailzustands-Automat) erzwingt der Kern mit 409 – hier nur Anzeige.
async function suspendTask(task, agentId) {
  await postTaskAction(task, agentId, "/suspend", "Aufgabe angehalten",
    ["Die Frist läuft weiter – Anhalten ist Transparenz, kein Fristen-Stopp."]);
}

async function resumeTask(task, agentId) {
  await postTaskAction(task, agentId, "/resume", "Weiter geht's");
}

function failTask(task, agentId) {
  const reason = el("input", { type: "text", placeholder: "z. B. Unterlagen unvollständig" });
  openModal(`Problem melden – ${task.label || task.node_id}`,
    el("div", null,
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Der Schritt wird als gescheitert markiert und wartet auf den Wiederanlauf – erst dann wird er wieder allen Berechtigten angeboten (mit frischer Frist). Der Vorgang bleibt dabei in einem definierten Zustand."),
      el("label", { class: "field" }, "Begründung", reason)),
    async () => {
      if (!reason.value.trim()) { toast("err", "Bitte eine Begründung angeben"); return false; }
      return postTaskAction(task, agentId, "/fail", "Problem gemeldet", undefined,
        { reason: reason.value.trim() });
    }, "Melden");
}

async function resetTask(task, agentId) {
  await postTaskAction(task, agentId, "/reset", "Wiederanlauf",
    ["Die Aufgabe wird wieder allen Berechtigten angeboten – mit frischer Frist."]);
}

async function completeTask(task, agentId) {
  // Die Instanz wird ohnehin geladen (fuer ein moegliches Ad-hoc-Schema); ihre
  // Datenwerte belegen zugleich die Maske vor -- siehe promptComplete.
  let schema;
  let inst;
  try {
    inst = await api.get(`/instances/${task.instance_id}`);
    schema = inst.ad_hoc_schema || await api.get(`/schemas/${task.schema_id}`);
  } catch (err) { toastError(err); return; }
  await promptComplete(schema, task.instance_id, task.node_id, task.label || task.node_id, agentId,
    async () => { render(); }, inst.data_values);
}

// --------------------------------------------------------------------------
// View: Prüfinstanz (4-Quadranten-Analyse-Cockpit für einen Entwurf)
// --------------------------------------------------------------------------
//
// Der Modellierer startet aus der Modellieransicht eine Prüfinstanz -- eine
// Test-Instanz eines noch nicht freigegebenen Entwurfs -- und spielt sie hier
// durch, um das Modellkonzept zu erarbeiten. Das Fenster ist in vier
// Quadranten geteilt:
//   oben links   Monitoring, beschränkt auf DIESE eine Instanz (Prozesskarte,
//                Fortschritt, Instanzdaten, Audit-Verlauf);
//   oben rechts  der angemeldete Starter (wer die Instanz gestartet hat);
//   unten        zwei frei wählbare, an der Instanz beteiligte Agenten, jeweils
//                mit ihrer auf diese Instanz gefilterten Arbeitsliste.
// Wie der ganze Client trägt die Sicht KEINE Korrektheitslogik: sie ruft nur
// geprüfte Endpunkte. Die instanzgefilterte Arbeitsliste entsteht rein
// clientseitig aus GET /instances/{id}/tasks (OpenTask.eligible_agents).

async function viewTestRun() {
  const content = byId("content");
  clear(content);
  if (!state.schema) { content.appendChild(emptyState("Kein Schema ausgew\u00E4hlt.")); return; }
  const schema = state.schema;

  if (!state.testInstanceId) {
    renderTestStartCard(content, schema);
    content.appendChild(simulationPanel(schema));
    return;
  }

  let inst;
  try { inst = await loadTestInstance(state.testInstanceId); }
  catch (err) {
    // Instanz verschwunden (z. B. Neustart des in-memory-Kerns) -> Startkarte.
    state.testInstanceId = null; state.testInstance = null; persistTestState();
    renderTestStartCard(content, schema,
      "Die zuletzt genutzte Pr\u00FCfinstanz ist nicht mehr verf\u00FCgbar (z. B. nach einem Neustart des Kerns). Bitte neu starten.");
    return;
  }

  const runSchema = inst.ad_hoc_schema || schema;
  const org = runSchema.org_model || { agents: {} };
  const agents = Object.values(org.agents || {}).sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  ensureTestAgentDefaults(agents);

  // Offene Aufgaben der Instanz EINMAL laden -> je Agent gefiltert dargestellt.
  let instanceTasks = [];
  try { instanceTasks = await api.get(`/instances/${inst.id}/tasks`); } catch (e) { /* ignore */ }

  content.appendChild(testHeader(schema, inst));
  content.appendChild(el("div", { class: "quad-grid" },
    el("div", { class: "quad-cell" }, testMonitorPanel(inst, runSchema)),
    el("div", { class: "quad-cell" }, testStarterPanel(runSchema)),
    el("div", { class: "quad-cell" }, testAgentPanel("A", inst, runSchema, agents, instanceTasks)),
    el("div", { class: "quad-cell" }, testAgentPanel("B", inst, runSchema, agents, instanceTasks))));
  content.appendChild(simulationPanel(runSchema));
}

// --- Simulation (E6): seiteneffektfreier Was-wäre-wenn-Durchlauf -----------
// Ergänzt die interaktive Prüfinstanz um die schnelle Vorab-Frage „Welchen
// Weg nimmt der Prozess mit diesen Werten?“. Jeder Lauf ist ein frischer
// Aufruf von POST /schemas/{id}/simulate – der Kern spielt rein und ohne
// jede Spur durch (kein Store, kein Audit, keine Mails); der Client zeigt
// das Ergebnis mit demselben renderGraph wie jede Laufzeit-Sicht.
//
// Eingaben und letztes Ergebnis liegen in ``state.simulation`` (je Schema),
// nicht nur im DOM: Die Pruefinstanz-Sicht zeichnet sich bei jedem Fortschritt
// und alle 30 s neu, und das Ergebnis verschwand sonst ungefragt.
/**
 * @param {object} schema das simulierte Schema (Entwurf oder Ad-hoc-Variante)
 * @returns {HTMLElement} das Panel
 */
function simulationPanel(schema) {
  const key = schema.id || state.schemaId;
  // Fingerabdruck des Modellstands: Aendert sich der Entwurf (Zweig geloescht,
  // Schritt eingefuegt), passt ein altes Ergebnis nicht mehr zum Graphen --
  // es wird verworfen; die Eingaben bleiben als Vorschlag stehen.
  const fp = JSON.stringify([schema.nodes, schema.edges, schema.data_elements,
    schema.data_accesses, schema.version]);
  if (!state.simulation || state.simulation.key !== key) {
    state.simulation = { key, fp, values: {}, result: null, runId: 0 };
  } else if (state.simulation.fp !== fp) {
    state.simulation.fp = fp;
    state.simulation.result = null;
    state.simulation.runId = (state.simulation.runId || 0) + 1;  // laufende Antwort ist veraltet
  }
  const sim0 = state.simulation;
  const elems = Object.values(schema.data_elements || {})
    .filter((d) => d.source === "INSTANCE")
    .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  const inputs = {};
  const fields = elems.map((d) => {
    let input;
    if (d.data_type === "BOOLEAN") {
      input = el("select", null,
        el("option", { value: "" }, "– nicht gesetzt –"),
        el("option", { value: "true" }, "wahr"),
        el("option", { value: "false" }, "falsch"));
    } else if (isNumericType(d.data_type)) {
      input = el("input", { type: "number", placeholder: "– nicht gesetzt –" });
    } else {
      input = el("input", { type: "text", placeholder: "– nicht gesetzt –" });
    }
    inputs[d.id] = { input, type: d.data_type };
    // Wert aus state wiederherstellen und dort mitfuehren; data-live-safe:
    // diese Eingabe haelt die Live-Aktualisierung nicht auf (sie uebersteht sie).
    input.setAttribute("data-live-safe", "1");
    if (sim0.values[d.id] != null) input.value = sim0.values[d.id];
    const keep = () => { sim0.values[d.id] = input.value; };
    input.addEventListener("input", keep);
    input.addEventListener("change", keep);
    return el("label", { class: "field" }, `${d.name} (${typeName(d.data_type)})`, input);
  });
  // data-sim-result: Kommt die Antwort erst, nachdem die Sicht neu gezeichnet
  // wurde, landet sie im dann sichtbaren Panel statt im verworfenen.
  const result = el("div", { "data-sim-result": "1" });
  if (sim0.result) result.appendChild(renderSimulationResult(schema, sim0.result));
  async function run() {
    const data = {};
    Object.entries(inputs).forEach(([id, { input, type }]) => {
      const v = (input.value || "").trim();
      if (v === "") return;  // leer = nicht gesetzt (bewusst)
      if (type === "BOOLEAN") data[id] = v === "true";
      else data[id] = coerceTypedInput(type, v);  // Zahlen inkl. Betrag
    });
    clear(result);
    sim0.result = null;
    const runId = (sim0.runId || 0) + 1;
    sim0.runId = runId;
    try {
      const sim = await api.post(`/schemas/${state.schemaId}/simulate`, { data });
      if (sim0.runId !== runId || state.simulation !== sim0) return;  // ueberholt
      sim0.result = sim;
      const target = result.isConnected ? result : document.querySelector("[data-sim-result]");
      if (target) { clear(target); target.appendChild(renderSimulationResult(schema, sim)); }
    } catch (err) { toastError(err); }
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Simulation"),
      el("span", { class: "sub" }, "Was-wäre-wenn: Weg und Dauer mit diesen Werten – ohne Seiteneffekte")),
    el("div", { class: "panel-b" },
      el("div", { class: "muted", style: "font-size:12px;margin-bottom:8px" },
        "Die Simulation spielt den Prozess mit den angegebenen Werten automatisch durch – gewählte Zweige, Schleifenrunden, erwartete Dauer. Es wird nichts gespeichert; fehlt ein entscheidender Wert, bricht der Lauf mit Begründung ab."),
      fields.length
        ? el("div", { class: "form-grid" }, ...fields)
        : el("div", { class: "muted", style: "font-size:13px" }, "Keine Instanz-Datenelemente – die Simulation läuft ohne Eingaben."),
      el("div", { class: "row", style: "margin-top:8px" },
        el("button", { class: "btn small primary", onClick: run }, "Simulieren")),
      result));
}

function renderSimulationResult(schema, sim) {
  const box = el("div", { style: "margin-top:12px" });
  const badge = sim.completed
    ? el("span", { class: "pill pill-green" }, "Ende erreicht")
    : el("span", { class: "pill pill-red" }, "nicht bis zum Ende");
  const dur = sim.expected_duration_seconds != null
    ? formatDuration(sim.expected_duration_seconds) : "–";
  box.appendChild(el("div", { class: "row", style: "gap:8px;align-items:center;margin-bottom:8px" },
    badge,
    el("span", { class: "muted", style: "font-size:12px" }, `erwartete Dauer: ${dur}`),
    el("span", { class: "muted", style: "font-size:12px" }, `${countLabel((sim.executed || []).length, "Schritt", "Schritte")} ausgeführt`)));
  (sim.findings || []).forEach((f) => box.appendChild(el("div", { class: "warn-banner" }, f)));
  // Markierungsbild: dieselbe Kontrollfluss-Darstellung wie jede Laufzeit-
  // Sicht, gespeist aus der synthetischen Instanz des Simulationsergebnisses
  // (node_states + loop_iterations reichen renderGraph als „Instanz“).
  const graphWrap = renderGraph(schema, { instance: sim });
  // Abspiel-Animation (Stufe B): die Abschlussreihenfolge
  // ``executed`` Schritt für Schritt über dem Markierungsbild nachspielen.
  if ((sim.executed || []).length) {
    box.appendChild(simulationPlaybackControls(graphWrap, schema, sim.executed));
  }
  box.appendChild(graphWrap);
  const dec = Object.entries(sim.decisions || {});
  if (dec.length) {
    box.appendChild(el("div", { class: "sub-h", style: "margin-top:8px" }, el("h3", null, "Gewählte Zweige")));
    box.appendChild(table(["Verzweigung", "Zweig"], dec.map(([split, target]) => {
      const s = schema.nodes[split];
      return [s ? nodeCaptionInContext(schema, s) : split, branchCaption(schema, split, target)];
    })));
  }
  return box;
}

// Abspiel-Animation der Simulation (Stufe B): spielt die
// Abschlussreihenfolge ``executed`` über dem fertigen Markierungsbild nach –
// rein visuell im Client (CSS-Klassen auf den ``data-node-id``-Gruppen des
// SVG), kein weiterer Kern-Aufruf, keine erfundenen Zwischen-Markierungen:
// Zu Beginn werden alle Knoten gedimmt, dann leuchtet Schritt für Schritt der
// jeweils abgeschlossene auf und bleibt sichtbar; am Ende ist wieder exakt
// das ungefilterte Endbild zu sehen. Der Takt bricht selbstständig ab, wenn
// das SVG den DOM verlässt (Panel neu gerendert) – kein Timer-Leck.
const SIM_PLAY_TICK_MS = 700;

function simulationPlaybackControls(graphWrap, schema, executed) {
  const readout = el("span", { class: "muted", style: "font-size:12px" }, "");
  let timer = null;
  let btn = null;

  function groupOf(nodeId) {
    return graphWrap.querySelector(`g[data-node-id="${nodeId}"]`);
  }
  function allGroups() {
    return Array.from(graphWrap.querySelectorAll("g[data-node-id]"));
  }
  function stop(finished) {
    if (timer) { clearInterval(timer); timer = null; }
    allGroups().forEach((g) => g.classList.remove("sim-dim", "sim-current"));
    readout.textContent = finished ? `abgespielt – ${executed.length} Schritte` : "";
    btn.textContent = "▶ Abspielen";
  }
  function play() {
    if (timer) { stop(false); return; }  // der Knopf ist zugleich „Stopp"
    let i = -1;
    allGroups().forEach((g) => { g.classList.add("sim-dim"); g.classList.remove("sim-current"); });
    btn.textContent = "■ Stopp";
    const step = () => {
      if (!graphWrap.isConnected) { clearInterval(timer); timer = null; return; }
      if (i >= 0) {
        const prev = groupOf(executed[i]);
        if (prev) prev.classList.remove("sim-current");
      }
      i += 1;
      if (i >= executed.length) { stop(true); return; }
      const g = groupOf(executed[i]);
      if (g) {
        g.classList.remove("sim-dim");
        g.classList.add("sim-current");
      }
      const node = schema.nodes[executed[i]];
      readout.textContent = `Schritt ${i + 1}/${executed.length}: ${node ? nodeCaption(node) : executed[i]}`;
    };
    timer = setInterval(step, SIM_PLAY_TICK_MS);
    step();  // sofort starten, nicht erst nach dem ersten Takt
  }
  btn = el("button", { class: "btn small", onClick: play }, "▶ Abspielen");
  return el("div", { class: "row", style: "gap:8px;align-items:center;margin:4px 0" }, btn, readout);
}

// Startkarte, solange keine (gültige) Prüfinstanz geladen ist.
function renderTestStartCard(content, schema, note) {
  const draft = isDraft(schema);
  const canStart = draft && hasRole("modeler", "admin");
  const body = el("div", { class: "panel-b" },
    note ? el("div", { class: "muted", style: "margin-bottom:10px" }, note) : null,
    el("p", { class: "muted" },
      "Eine Pr\u00FCfinstanz ist eine Test-Instanz dieses Entwurfs. Sie k\u00F6nnen den Prozess hier durchspielen "
      + "und so das Modellkonzept erarbeiten \u2013 beim Start wird gefragt, wer die Instanz startet."),
    canStart
      ? el("div", { style: "margin-top:12px" },
        el("button", { class: "btn primary", onClick: startTestInstance }, "\u2697 Pr\u00FCfinstanz starten"))
      : el("div", { class: "muted" }, draft
        ? "Nur Modellierer/Administratoren k\u00F6nnen eine Pr\u00FCfinstanz starten."
        : "Das Schema ist bereits freigegeben. Pr\u00FCfinstanzen dienen der Analyse von Entw\u00FCrfen \u2013 "
          + "nutzen Sie f\u00FCr freigegebene Schemata die Ausf\u00FChrungs-/Monitoring-Sicht."));
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Pr\u00FCfinstanz"),
      el("span", { class: "sub" }, schema.name + " v" + schema.version), lifecyclePill(schema)),
    body));
}

// Prüfinstanz starten: fragt, wer sie startet, legt sie an und öffnet das Cockpit.
async function startTestInstance() {
  const schema = state.schema;
  if (!schema) return;
  if (!isDraft(schema)) {
    toast("err", "Nicht m\u00F6glich", ["Pr\u00FCfinstanzen sind nur f\u00FCr Entw\u00FCrfe (nicht freigegeben) vorgesehen."]);
    return;
  }
  const org = schema.org_model || { agents: {} };
  const agents = Object.values(org.agents || {}).sort((a, b) => (a.name || "").localeCompare(b.name || ""));
  if (!agents.length) {
    toast("err", "Keine Agenten", ["Legen Sie zuerst Agenten in der Ressourcensicht an."]);
    return;
  }
  const sel = el("select", null, ...agents.map((a) => el("option", { value: a.id }, a.name)));
  const body = el("div", { class: "form-grid" },
    el("p", { class: "muted", style: "margin:0 0 4px" },
      "W\u00E4hlen Sie, wer die Pr\u00FCfinstanz startet. Diese Person erscheint oben rechts als angemeldeter Starter."),
    el("label", { class: "field" }, "Startende Person", sel));
  openModal("Pr\u00FCfinstanz starten", body, async () => {
    try {
      const inst = await api.post(`/schemas/${state.schemaId}/instances`);
      state.testInstanceId = inst.id;
      state.testInstance = inst;
      state.testStarter = sel.value;
      state.testAgentA = null;
      state.testAgentB = null;
      persistTestState();
      state.view = "testrun";
      setActiveNav();
      render();
      toast("ok", "Pr\u00FCfinstanz gestartet", [`${instanceCaption(inst, schema)} \u00B7 ${inst.id}`]);
      return true;
    } catch (err) { toastError(err); return false; }
  }, "Starten");
}

async function loadTestInstance(id) {
  state.testInstanceId = id;
  state.testInstance = await api.get(`/instances/${id}`);
  return state.testInstance;
}

// Ungültige Agenten-Auswahl bereinigen und sinnvoll vorbelegen (erste zwei).
function ensureTestAgentDefaults(agents) {
  const ids = agents.map((a) => a.id);
  if (state.testAgentA && !ids.includes(state.testAgentA)) state.testAgentA = null;
  if (state.testAgentB && !ids.includes(state.testAgentB)) state.testAgentB = null;
  if (!state.testAgentA && ids.length) state.testAgentA = ids[0];
  if (!state.testAgentB && ids.length > 1) state.testAgentB = ids.find((id) => id !== state.testAgentA) || null;
  persistTestState();
}

function persistTestState() {
  const set = (k, v) => { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); };
  set("testInstanceId", state.testInstanceId);
  set("testStarter", state.testStarter);
  set("testAgentA", state.testAgentA);
  set("testAgentB", state.testAgentB);
}

function testHeader(schema, inst) {
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, "Pr\u00FCfinstanz"),
      el("span", { class: "sub" }, schema.name + " v" + schema.version),
      lifecyclePill(schema),
      el("span", { class: "pill pill-amber", title: "Test-Instanz eines Entwurfs \u2013 nicht Teil des echten Betriebs" }, "TEST"),
      statePillFor(inst.state),
      el("span", { class: "spacer", style: "flex:1" }),
      hasRole("modeler", "admin")
        ? el("button", { class: "btn small primary", onClick: startTestInstance }, "\u2697 Neue Pr\u00FCfinstanz")
        : null,
      el("button", { class: "btn small ghost", onClick: closeTestInstance }, "Analyse schlie\u00DFen")));
}

function closeTestInstance() {
  state.testInstanceId = null;
  state.testInstance = null;
  persistTestState();
  render();
}

// Oben links: Monitoring, beschränkt auf diese eine Instanz.
//
// Test-Instanzen erzeugen bewusst KEINE Audit-Events (sie sollen das globale
// Monitoring/die KPIs nie verf\u00E4lschen), daher speist sich diese Sicht rein aus
// dem Instanz-Objekt: node_states (Schrittfortschritt), performed_by (Bearbeiter)
// und data_values -- unabh\u00E4ngig vom Audit-Log.
function testMonitorPanel(inst, runSchema) {
  const steps = Object.values(runSchema.nodes)
    .filter((n) => n.type === "ACTIVITY" || n.type === "SUBPROCESS");
  const stateOf = (id) => (inst.node_states || {})[id] || "NOT_ACTIVATED";
  const done = steps.filter((n) => { const s = stateOf(n.id); return s === "COMPLETED" || s === "SKIPPED"; }).length;
  const total = steps.length || 1;
  const pct = Math.round((done / total) * 100);

  const kpis = el("div", { class: "kpis kpis-compact" },
    kpi("Status", INSTANCE_STATE_LABELS[inst.state] || inst.state),
    kpi("Fortschritt", pct + "%"),
    kpi("Schritte", done + "/" + steps.length),
    kpi("Datenwerte", Object.keys(inst.data_values || {}).length));

  const dataRows = Object.entries(inst.data_values || {}).map(([k, v]) => {
    const elem = runSchema.data_elements[k];
    return [elem ? elem.name : k, formatValue(elem, v)];
  });
  const dataBlock = el("div", { class: "panel-b" },
    el("div", { class: "sub-h" }, el("h3", null, "Instanzdaten")),
    dataRows.length ? table(["Element", "Wert"], dataRows) : emptyState("Noch keine Werte."));

  // Schrittübersicht aus den Knotenmarkierungen (audit-unabhängig).
  const stepBlock = el("div", { class: "panel-b" }, el("div", { class: "sub-h" }, el("h3", null, "Schritte")));
  if (!steps.length) {
    stepBlock.appendChild(emptyState("Keine Aktivit\u00E4ten im Modell."));
  } else {
    const rows = steps.map((n) => {
      const s = stateOf(n.id);
      const who = (inst.performed_by || {})[n.id];
      return [nodeCaption(n), nodeStatePill(s), who ? agentNameOfIn(runSchema, who) : "\u2013"];
    });
    stepBlock.appendChild(table(["Schritt", "Status", "Bearbeiter"], rows));
  }

  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Monitoring"),
      el("span", { class: "sub", title: `Kennung: ${inst.id}` }, instanceCaption(inst, runSchema)),
      inst.state === "COMPLETED"
        ? el("span", { class: "pill pill-green" }, "fertig")
        : el("span", { class: "pill pill-blue" }, "l\u00E4uft")),
    el("div", { class: "panel-b" }, kpis),
    el("div", { class: "panel-b" }, el("div", { class: "sub-h" }, el("h3", null, "Live-Prozesslandkarte")), renderGraph(runSchema, { instance: inst })),
    dataBlock, stepBlock);
}

// Farbige Statusmarke für eine Knotenmarkierung (NodeState) im Schritt-Panel.
const NODE_STATE_META = {
  NOT_ACTIVATED: { label: "wartet", cls: "" },
  ACTIVATED: { label: "bereit", cls: "pill-blue" },
  RUNNING: { label: "l\u00E4uft", cls: "pill-blue" },
  COMPLETED: { label: "erledigt", cls: "pill-green" },
  SKIPPED: { label: "\u00FCbersprungen", cls: "pill-amber" },
};
/**
 * Deutsche Bezeichnung einer Knotenmarkierung (``NodeState``), z. B. fuer die
 * Unterzeile der Prozesslandkarte. Leer bleibt leer.
 * @param {string} s Markierung
 * @returns {string}
 */
function nodeStateLabel(s) { return s ? ((NODE_STATE_META[s] || {}).label || s) : ""; }

function nodeStatePill(s) {
  const m = NODE_STATE_META[s] || { label: s, cls: "" };
  return el("span", { class: "pill " + m.cls }, m.label);
}

// Oben rechts: der angemeldete Starter (wer die Instanz gestartet hat).
function testStarterPanel(runSchema) {
  const org = runSchema.org_model || { agents: {} };
  const id = state.testStarter;
  const body = el("div", { class: "panel-b" });
  if (!id || !(org.agents || {})[id]) {
    body.appendChild(emptyState("Kein Starter erfasst."));
  } else {
    const info = agentIdentity(org, id);
    body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Angemeldet als " + info.name));
    body.appendChild(el("div", { class: "id-card" },
      idRow("Person", info.name),
      idRow("Rollen", info.roles || "\u2013"),
      idRow("Abteilung", info.unit || "\u2013")));
    body.appendChild(el("p", { class: "muted", style: "margin-top:12px; font-size:12px" },
      "Diese Person hat die Pr\u00FCfinstanz gestartet. Im Anmeldebetrieb entspr\u00E4che dies der angemeldeten Kennung."));
  }
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Starter"), el("span", { class: "sub" }, "wer die Instanz gestartet hat")),
    body);
}

// Unten: ein frei wählbarer beteiligter Agent mit seiner instanzgefilterten
// Arbeitsliste. ``slot`` ist "A" (links) oder "B" (rechts).
function testAgentPanel(slot, inst, runSchema, agents, instanceTasks) {
  const org = runSchema.org_model || { agents: {} };
  const current = slot === "A" ? state.testAgentA : state.testAgentB;
  const sel = el("select", null,
    el("option", { value: "" }, "\u2013 Agent w\u00E4hlen \u2013"),
    ...agents.map((a) => el("option", { value: a.id }, a.name)));
  sel.value = current || "";
  sel.addEventListener("change", () => {
    if (slot === "A") state.testAgentA = sel.value || null;
    else state.testAgentB = sel.value || null;
    persistTestState();
    render();
  });

  const body = el("div", { class: "panel-b" },
    el("label", { class: "field" }, "Beteiligter Agent (frei w\u00E4hlbar)", sel));

  if (current && (org.agents || {})[current]) {
    const info = agentIdentity(org, current);
    const meta = [info.roles ? "Rollen: " + info.roles : null, info.unit || null].filter(Boolean).join(" \u00B7 ");
    if (meta) body.appendChild(el("div", { class: "muted", style: "font-size:12px; margin:2px 0 10px" }, meta));

    if (inst.state === "COMPLETED") {
      body.appendChild(el("div", { class: "ok-banner" }, "\u2713 Instanz abgeschlossen \u2013 keine offenen Aufgaben."));
    } else {
      const mine = (instanceTasks || []).filter((t) => (t.eligible_agents || []).includes(current));
      if (!mine.length) {
        body.appendChild(el("div", { class: "muted", style: "font-size:13px" },
          "Keine offenen Aufgaben f\u00FCr " + info.name + " in dieser Pr\u00FCfinstanz."));
      } else {
        mine.forEach((t) => {
          const node = runSchema.nodes[t.node_id];
          const badge = criticalityBadge(t);
          const item = el("div", { class: "worklist-item" },
            el("span", { class: "name" }, t.label || (node ? nodeCaption(node) : t.node_id)),
            el("span", { class: "tag" }, priorityShort(t.priority)));
          if (badge) item.appendChild(badge);
          item.appendChild(el("button", { class: "btn small green", onClick: () => completeTestTask(inst, runSchema, t, current) }, "Erledigen"));
          body.appendChild(item);
        });
      }
    }
  } else {
    body.appendChild(el("div", { class: "muted", style: "font-size:13px" },
      "W\u00E4hlen Sie einen beteiligten Agenten, um dessen auf diese Pr\u00FCfinstanz gefilterte Arbeitsliste zu sehen."));
  }

  const who = current && (org.agents || {})[current] ? agentIdentity(org, current).name : "kein Agent";
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Arbeitsliste " + slot), el("span", { class: "sub" }, who)),
    body);
}

async function completeTestTask(inst, schema, task, agentId) {
  await promptComplete(schema, inst.id, task.node_id, task.label || task.node_id, agentId, async () => {
    await loadTestInstance(inst.id);
    render();
  }, inst.data_values);
}

// Identität eines Agenten (Name, Rollen, Abteilung) aus einem gegebenen
// Organisationsmodell -- robust auch für Ad-hoc-Instanzschemata.
function agentIdentity(org, agentId) {
  const a = org && org.agents ? org.agents[agentId] : null;
  if (!a) return { name: agentId || "\u2013", roles: "", unit: "" };
  const roles = (a.role_ids || []).map((r) => (org.roles && org.roles[r] ? org.roles[r].name : r)).join(", ");
  const unit = a.org_unit_id && org.org_units && org.org_units[a.org_unit_id] ? org.org_units[a.org_unit_id].name : "";
  return { name: a.name, roles, unit };
}

function idRow(label, value) {
  return el("div", { class: "id-row" },
    el("span", { class: "id-label" }, label), el("span", { class: "id-value" }, value));
}

// Agentenname aus einem konkreten Schema (statt dem global geladenen state.schema).
function agentNameOfIn(schema, id) {
  const org = schema && schema.org_model;
  const a = org && org.agents ? org.agents[id] : null;
  return a ? a.name : id;
}

// Kurzform der abgeleiteten Arbeitslisten-Priorität (E8) für die Aufgabenkachel.
const PRIORITY_LABELS = { LOW: "niedrig", MEDIUM: "normal", HIGH: "hoch", CRITICAL: "kritisch" };
function priorityShort(p) { return PRIORITY_LABELS[p] || "bereit"; }

// --- Zeitbasierte, automatische Arbeitslisten-Priorisierung ----------------
// Rein darstellend: der Server liefert je Aufgabe (OpenTask) das abgeleitete
// Kritikalitätsband und die Restzeit; hier werden sie nur zu Text/Badge. Keine
// Korrektheitslogik im Client.

//: Anwenderverständliche Beschriftung je Kritikalitätsband.
const CRITICALITY_LABELS = {
  ON_TRACK: "im Plan", WARNING: "wird knapp", AT_RISK: "gefährdet", OVERDUE: "überfällig", NONE: "",
};
//: CSS-Klasse (Ampelfarbe) je Band; NONE bleibt ohne Badge.
const CRITICALITY_CLASS = {
  ON_TRACK: "crit-ontrack", WARNING: "crit-warning", AT_RISK: "crit-atrisk", OVERDUE: "crit-overdue",
};

// Ein farbiges Kritikalitäts-Badge für eine Aufgabe, oder null ohne Soll-Zeit.
function criticalityBadge(t) {
  const band = (t && t.time_criticality) || "NONE";
  if (band === "NONE" || !CRITICALITY_LABELS[band]) return null;
  return el("span", { class: "badge " + (CRITICALITY_CLASS[band] || "") }, CRITICALITY_LABELS[band]);
}

// Menschliche Dauer (Sekunden -> "12 Min." / "2 Std. 5 Min." / "3 Tg.").
function humanDuration(sec) {
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + " Sek.";
  const min = Math.round(sec / 60);
  if (min < 60) return min + " Min.";
  const h = Math.floor(min / 60), m = min % 60;
  if (h < 24) return m ? (h + " Std. " + m + " Min.") : (h + " Std.");
  return Math.floor(h / 24) + " Tg.";
}

// Relative Fälligkeit einer Aufgabe ("in 12 Min." / "überfällig seit 3 Min.").
// "–" (Gedankenstrich), wenn die Aufgabe keine Soll-Zeit trägt.
function dueLabel(t) {
  if (!t || t.remaining_seconds == null) return "–";
  const s = Math.round(t.remaining_seconds);
  return s < 0 ? ("überfällig seit " + humanDuration(-s)) : ("in " + humanDuration(s));
}

// Die "Fällig"-Zelle: relative Restzeit plus farbiges Band-Badge und – bei
// vorhandener Soll-Zeit – der ρ-Verbrauchsbalken (Z4):
// wie viel der Soll-Zeit ist verbraucht, Farbe = Band. Rein
// abgeleitet aus den API-Feldern, kein eigener Zustand.
function dueCell(t) {
  const badge = criticalityBadge(t);
  const label = el("span", { class: "due-label" }, dueLabel(t));
  const cell = badge ? el("span", { class: "due-cell" }, label, badge) : label;
  if (t && t.target_seconds > 0 && t.elapsed_seconds != null) {
    const ratio = Math.max(0, Math.min(1, t.elapsed_seconds / t.target_seconds));
    const band = (t.time_criticality || "NONE").toLowerCase();
    return el("span", { class: "due-wrap" }, cell,
      el("span", { class: "rho-bar", title: Math.round(ratio * 100) + "% der Soll-Zeit verbraucht" },
        el("span", { class: "rho-fill rho-" + band, style: `width:${Math.round(ratio * 100)}%` })));
  }
  return cell;
}

// --------------------------------------------------------------------------
// View: Integration (Roadmap P5)
// --------------------------------------------------------------------------
//
// Wie der ganze Client traegt diese Sicht KEINE Korrektheitslogik: sie ruft
// nur gepruefte Endpunkte (Connectoren, Datenanbindung, Automatik, Webhooks).
// Validitaet ist eine Eigenschaft des Serverzustands; ungueltige Eingaben
// weist der Kern mit 422 ab und wir zeigen den Befund als Toast.

async function viewIntegration() {
  const content = byId("content");
  clear(content);
  // Connectoren + Webhooks parallel laden (unabhaengige Endpunkte).
  const [connPanel, hookPanel] = await Promise.all([
    connectorRegistryPanel(),
    webhookPanel(),
  ]);
  content.appendChild(connPanel);          // 11.1
  content.appendChild(dataBindingPanel()); // 11.2
  content.appendChild(automationPanel());  // 11.3
  content.appendChild(hookPanel);          // 11.4
}

// --- 11.1 Connector-Registry ----------------------------------------------

async function connectorRegistryPanel() {
  let connectors = [];
  try { connectors = await api.get("/v1/connectors"); }
  catch (err) { toastError(err); }
  const rows = connectors.map((c) => {
    const st = state.connectorStatus[c.connector_id] || "unknown";
    const pill = el("span", { class: "pill " + (st === "ok" ? "pill-green" : st === "err" ? "pill-red" : "pill-gray") },
      st === "ok" ? "verbunden" : st === "err" ? "Fehler" : "ungepr\u00FCft");
    const testBtn = el("button", { class: "btn small", onClick: () => testConnector(c.connector_id) }, "Verbindung testen");
    const readBtn = el("button", { class: "btn small ghost", onClick: () => sampleReadConnector(c.connector_id) }, "Testlesen");
    return [c.connector_id, el("span", { class: "pill pill-blue" }, c.kind), pill,
      el("div", { class: "row-actions" }, testBtn, readBtn)];
  });
  const body = el("div", { class: "panel-b" },
    connectors.length
      ? table(["Connector", "Typ", "Status", ""], rows)
      : emptyState("Keine Connectoren konfiguriert. Connectoren werden serverseitig (admin-only) eingerichtet \u2013 Zugangsdaten bleiben dort, niemals im Modell."),
    el("p", { class: "muted", style: "margin-top:10px" },
      "Zugangsdaten werden nie angezeigt oder abgefragt. Die Modellierung referenziert nur eine serverseitig aufgel\u00F6ste Secret-Referenz."));
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Connector-Registry"), el("span", { class: "sub" }, "Externe Datensysteme \u00B7 nur Metadaten")),
    body);
}

async function testConnector(id) {
  try {
    const res = await api.post(`/v1/connectors/${id}/test`);
    state.connectorStatus[id] = res.ok ? "ok" : "err";
    toast(res.ok ? "ok" : "err", res.ok ? "Verbindung ok" : "Verbindung fehlgeschlagen", [id]);
  } catch (err) {
    state.connectorStatus[id] = "err";
    toastError(err);
  }
  render();
}

/**
 * Fuellt eine Datalist mit den Entitaeten (Tabellen/Sichten) eines Connectors.
 *
 * Der Katalog kommt aus ``GET /v1/connectors/{id}/entities`` (reine Metadaten,
 * keine Zeile wird gelesen). Schlaegt der Abruf fehl oder kennt der Connector
 * keinen Katalog, bleibt das zugehoerige Feld ein gewoehnliches Freitextfeld --
 * die Vorschlagsliste ist eine Hilfe, nie eine Voraussetzung.
 *
 * @param {HTMLElement} datalistEl die zu fuellende <datalist>
 * @param {string} connectorId Connector, dessen Katalog gelesen wird
 * @returns {Promise<string[]>} die angebotenen Namen (leer, wenn keine)
 */
async function fillEntitySuggestions(datalistEl, connectorId) {
  clear(datalistEl);
  if (!connectorId) return [];
  let names = [];
  try { names = await api.get(`/v1/connectors/${connectorId}/entities`); }
  catch (e) { return []; }  // best effort: ohne Katalog bleibt das Freitextfeld
  names.forEach((n) => datalistEl.appendChild(el("option", { value: n })));
  return names;
}

/**
 * Haengt an ein Entitaets-Feld die Tabellenliste seines Connectors.
 *
 * Gibt die zu ergaenzende <datalist> zurueck (sie muss im Dialog haengen, damit
 * der Browser sie findet). Wechselt die Connector-Auswahl, wird der Katalog neu
 * geladen. Bietet der Connector genau eine Tabelle an, wird sie in ein noch
 * leeres Feld eingetragen -- der haeufigste Fall, und niemand muss den Namen
 * raten.
 *
 * @param {HTMLInputElement} entityInput das Freitextfeld der Entitaet
 * @param {string} listId eindeutige Id der Datalist (pro Dialog eine)
 * @param {() => string} getConnectorId liefert die aktuelle Connector-Id
 * @param {HTMLSelectElement} [connSelect] Auswahlfeld, das den Katalog wechselt
 * @returns {HTMLElement} die <datalist>
 */
function wireEntitySuggestions(entityInput, listId, getConnectorId, connSelect) {
  const list = el("datalist", { id: listId });
  entityInput.setAttribute("list", listId);
  const reload = () => fillEntitySuggestions(list, getConnectorId()).then((names) => {
    if (names.length === 1 && !entityInput.value) entityInput.value = names[0];
  });
  if (connSelect) connSelect.addEventListener("change", reload);
  reload();
  return list;
}

/**
 * Testlesen eines Connectors: Entitaet waehlen, Ergebnis IM Dialog anzeigen.
 *
 * Das Ergebnis bleibt bewusst im selben Dialog. Vorher oeffnete der Rueckruf
 * ein zweites Fenster im selben Modal-Container, das ``openModal`` unmittelbar
 * danach wieder leerte (der Rueckruf gab nicht ``false`` zurueck) -- man sah
 * deshalb nie einen Datensatz, obwohl der Server sie lieferte.
 * Mit ``return false`` bleibt der Dialog stehen, und man kann eine andere
 * Tabelle lesen, ohne ihn neu zu oeffnen.
 *
 * @param {string} id Connector-Id aus der Registry
 */
function sampleReadConnector(id) {
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const list = wireEntitySuggestions(entity, "pw-sample-entities", () => id);
  const limit = el("input", { type: "number", value: "5", min: "1", max: "100" });
  const out = el("div", { class: "sample-out" });
  openModal("Testlesen \u2013 " + id, el("div", { class: "form-grid" },
    el("label", { class: "field wide" }, "Entit\u00E4t/Tabelle", entity, list,
      el("span", { class: "field-help" }, "Tabellen des Connectors werden vorgeschlagen, sobald der Katalog gelesen ist.")),
    el("label", { class: "field" }, "Anzahl", limit),
    out), async () => {
    if (!entity.value.trim()) { toast("info", "Bitte eine Entit\u00E4t/Tabelle angeben"); return false; }
    try {
      const rows = await api.post(`/v1/connectors/${id}/sample-read`,
        { entity: entity.value.trim(), limit: Number(limit.value) || 1 });
      renderSampleRecords(out, entity.value.trim(), rows);
    } catch (err) { toastError(err); }
    return false;  // Ergebnis steht im Dialog -- er darf sich nicht schliessen
  }, "Lesen");
}

/**
 * Zeichnet die gelesenen Beispieldatensaetze als Tabelle in einen Bereich.
 *
 * Spalten sind die Vereinigung aller Schluessel (ein Datensatz kann ein Feld
 * ausgelassen haben), Werte werden als Text dargestellt; ``null`` erscheint als
 * Gedankenstrich statt als "null".
 *
 * @param {HTMLElement} target Zielbereich (wird geleert)
 * @param {string} entity gelesene Entitaet, fuer die Ueberschrift
 * @param {object[]} rows Datensaetze der API
 */
function renderSampleRecords(target, entity, rows) {
  clear(target);
  target.appendChild(el("div", { class: "sub-h" },
    el("h3", null, `Beispieldatens\u00E4tze: ${entity} (${rows.length})`)));
  if (!rows.length) {
    target.appendChild(emptyState("Keine Datens\u00E4tze gefunden."));
    return;
  }
  const cols = [];
  rows.forEach((r) => Object.keys(r || {}).forEach((k) => { if (!cols.includes(k)) cols.push(k); }));
  target.appendChild(table(cols, rows.map((r) =>
    cols.map((c) => (r[c] === null || r[c] === undefined ? "\u2013" : String(r[c]))))));
}

// --- 11.2 Datenanbindungs-Assistent ---------------------------------------

function elemName(schema, id) {
  const e = schema.data_elements[id];
  return e ? e.name : id;
}

function dataBindingPanel() {
  const head = el("div", { class: "panel-h" },
    el("h2", null, "Datenanbindung (extern)"),
    el("span", { class: "sub" }, "Datenelemente an Connectoren binden (C1\u2013C6)"));
  if (!state.schema) {
    return el("div", { class: "panel" }, head,
      el("div", { class: "panel-b" }, emptyState("Kein Schema ausgew\u00E4hlt \u2013 oben ein Schema w\u00E4hlen.")));
  }
  const schema = state.schema;
  const draft = isDraft(schema);

  // Modell-Connectoren (Metadaten im Schema, getrennt von der Laufzeit-Registry).
  const connRows = Object.values(schema.connectors || {}).map((c) =>
    [c.name, el("span", { class: "pill pill-blue" }, c.kind), c.id]);
  const addConnBtn = el("button", { class: "btn small", onClick: registerSchemaConnector, ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "+ Connector");
  const connBlock = el("div", null,
    el("div", { class: "sub-h" }, el("h3", null, "Modell-Connectoren"), el("span", { class: "spacer", style: "flex:1" }), addConnBtn),
    connRows.length ? table(["Name", "Typ", "ID"], connRows)
      : emptyState("Noch keine Connectoren im Modell registriert."));

  // Datenelemente + externe Bindung.
  const hasConn = Object.keys(schema.connectors || {}).length > 0;
  const elemRows = Object.values(schema.data_elements).map((d) => {
    const src = d.source === "EXTERNAL"
      ? el("span", { class: "pill pill-amber" }, "EXTERN")
      : el("span", { class: "pill pill-gray" }, "Instanz");
    let detail = "\u2013";
    if (d.external) {
      detail = `${d.external.connector_id} \u00B7 ${d.external.entity} \u00B7 Schl\u00FCssel ${elemName(schema, d.external.key_element_id)}`;
    } else if (d.select) {
      const proj = d.select.aggregate && d.select.aggregate !== "NONE"
        ? `${d.select.aggregate}(${d.select.column})` : d.select.column;
      detail = `${d.select.connector_id} \u00B7 SELECT ${proj} FROM ${d.select.entity}`;
    } else if (d.write) {
      detail = `${d.write.connector_id} \u00B7 UPDATE ${d.write.entity} SET ${d.write.column}`;
    }
    const connLock = lockedBy([!draft, DRAFT_ONLY_REASON],
      [!hasConn, "Erst oben unter „Connectoren“ einen Connector anlegen."]);
    const bindBtn = el("button", { class: "btn small ghost", onClick: () => bindExternalElement(d), ...connLock }, "Datensatz");
    const sqlBtn = el("button", { class: "btn small ghost", onClick: () => bindSqlSelect(d), ...connLock }, "SQL-Select");
    const writeBtn = el("button", { class: "btn small ghost", onClick: () => bindSqlWrite(d), ...connLock }, "SQL-Write");
    return [d.name, typeName(d.data_type), src, detail, el("div", { class: "row-actions" }, bindBtn, sqlBtn, writeBtn)];
  });
  const elemBlock = el("div", null,
    el("div", { class: "sub-h" }, el("h3", null, "Datenelemente")),
    elemRows.length ? table(["Element", "Typ", "Quelle", "Abbildung", ""], elemRows)
      : emptyState("Noch keine Datenelemente. Lege sie in der Datensicht an."));

  return el("div", { class: "panel" }, head,
    el("div", { class: "panel-b" }, connBlock, el("div", { style: "height:14px" }), elemBlock,
      el("p", { class: "muted", style: "margin-top:10px" },
        "Lese-/Schreib-Richtung (D/C) wird \u00FCber die Bindungen in der Datensicht festgelegt.")));
}

function registerSchemaConnector() {
  const name = el("input", { type: "text", placeholder: "z. B. ERP-Kunden" });
  const kind = el("select", null, ...CONNECTOR_KINDS.map((k) => el("option", { value: k }, k)));
  const cid = el("input", { type: "text", placeholder: "optionale ID, z. B. erp" });
  openModal("Connector registrieren", el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Name", name),
    el("label", { class: "field" }, "Typ", kind),
    el("label", { class: "field" }, "ID (optional)", cid)), async () => {
    if (!name.value.trim()) return false;
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/connectors`,
      { name: name.value.trim(), kind: kind.value, connector_id: cid.value.trim() || null }), "Connector registriert");
  }, "Registrieren");
}

function bindExternalElement(element) {
  const schema = state.schema;
  const conn = el("select", null, ...Object.values(schema.connectors || {}).map((c) =>
    el("option", { value: c.id }, `${c.name} (${c.id})`)));
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const entityList = wireEntitySuggestions(entity, "pw-ext-entities", () => conn.value, conn);
  const keyElems = Object.values(schema.data_elements).filter((d) => d.source !== "EXTERNAL" && d.id !== element.id);
  const key = el("select", null, ...keyElems.map((d) => el("option", { value: d.id }, d.name)));
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Connector", conn),
    el("label", { class: "field wide" }, "Entit\u00E4t/Tabelle", entity, entityList),
    el("label", { class: "field" }, "Schl\u00FCssel-Datenelement", keyElems.length
      ? key
      : el("span", { class: "muted" }, "Erst ein Instanz-Datenelement anlegen.")));
  openModal(`Extern anbinden \u2013 ${element.name}`, body, async () => {
    if (!entity.value.trim() || !conn.value || !keyElems.length) return false;
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${element.id}/external`,
      { connector_id: conn.value, entity: entity.value.trim(), key_element_id: key.value }), "Datenelement extern angebunden");
  }, "Anbinden");
}

// --- 11.2b SQL-Select-Assistent (C4-C6) -----------------------------------

function sqlQuoteIdent(name) { return '"' + name + '"'; }
function sqlOpText(op) {
  return { EQ: "=", NE: "<>", LT: "<", LE: "<=", GT: ">", GE: ">=", LIKE: "LIKE", IN: "IN" }[op];
}
function sqlResultType(aggregate, columnType) {
  return aggregate === "COUNT" ? "INTEGER" : aggregate === "AVG" ? "FLOAT" : columnType;
}
// Client-side mirror of compile_select for the live preview (the server stays
// the sole authority; a wrong binding is rejected with 422 + C4-C6 findings).
function sqlSelectPreview(s) {
  const proj = s.aggregate === "NONE" ? sqlQuoteIdent(s.column) : `${s.aggregate}(${sqlQuoteIdent(s.column)})`;
  let sql = `SELECT ${proj} FROM ${s.entity.split(".").map(sqlQuoteIdent).join(".")}`;
  const where = s.filters.map((f, i) => `${sqlQuoteIdent(f.column)} ${sqlOpText(f.operator)} :f${i}`);
  if (where.length) sql += " WHERE " + where.join(" AND ");
  if (s.cardinality === "FIRST_ORDERED" && s.order_by.length) {
    sql += " ORDER BY " + s.order_by.map((o) => sqlQuoteIdent(o.column) + (o.descending ? " DESC" : "")).join(", ") + " LIMIT 1";
  }
  return sql;
}

function bindSqlSelect(element) {
  const schema = state.schema;
  const conns = Object.values(schema.connectors || {});
  const instanceElems = Object.values(schema.data_elements).filter((d) => d.source !== "EXTERNAL" && d.id !== element.id);
  const sourceType = (id) => { const e = schema.data_elements[id]; return e ? e.data_type : null; };

  const conn = el("select", null, ...conns.map((c) => el("option", { value: c.id }, `${c.name} (${c.id})`)));
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const entityList = wireEntitySuggestions(entity, "pw-sql-entities", () => conn.value, conn);
  const colInput = el("input", { type: "text", placeholder: "z. B. name", list: "pw-sql-cols" });
  const colDatalist = el("datalist", { id: "pw-sql-cols" });
  const colType = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t }, typeName(t))));
  colType.value = element.data_type;
  const agg = el("select", null, ...SQL_AGGREGATES.map((a) => el("option", { value: a }, a === "NONE" ? "\u2014 kein \u2014" : a)));
  const card = el("select", null, ...SQL_CARDINALITIES.map(([v, l]) => el("option", { value: v }, l)));
  const uniqueCol = el("input", { type: "text", placeholder: "z. B. kd_id" });
  const orderCol = el("input", { type: "text", placeholder: "Sortierspalte" });
  const orderDesc = el("input", { type: "checkbox" });
  const preview = el("pre", { class: "code-block" });
  const typeHint = el("div", { class: "sub" });
  const cardHint = el("div", { class: "sub" });
  const filtersBox = el("div", null);
  let columns = null;
  const filters = [];

  function applyColType() {
    if (!columns) return;
    const hit = columns.find((c) => c.column === colInput.value.trim());
    if (hit && hit.data_type) colType.value = hit.data_type;
  }
  async function loadColumns() {
    if (!conn.value || !entity.value.trim()) { toast("info", "Erst Connector und Entit\u00E4t w\u00E4hlen"); return; }
    try {
      columns = await api.get(`/v1/connectors/${conn.value}/columns?entity=${encodeURIComponent(entity.value.trim())}`);
      clear(colDatalist);
      columns.forEach((c) => colDatalist.appendChild(el("option", { value: c.column }, `${c.sql_type} \u2192 ${typeName(c.data_type)}`)));
      applyColType(); refresh();
      toast("ok", `${columns.length} Spalten geladen`);
    } catch (err) { columns = null; const d = describeError(err); toast("info", "Keine Live-Spalten \u2013 Namen/Typ manuell", d.lines); }
  }

  function spec() {
    return {
      connector_id: conn.value,
      entity: entity.value.trim(),
      column: colInput.value.trim(),
      column_type: colType.value,
      aggregate: agg.value,
      filters: filters.filter((f) => f.column.trim() && f.source)
        .map((f) => ({ column: f.column.trim(), column_type: sourceType(f.source), operator: f.operator, key_element_id: f.source })),
      cardinality: card.value,
      order_by: (card.value === "FIRST_ORDERED" && orderCol.value.trim())
        ? [{ column: orderCol.value.trim(), descending: orderDesc.checked }] : [],
      unique_column: uniqueCol.value.trim(),
    };
  }
  function refresh() {
    const s = spec();
    preview.textContent = (s.column && s.entity) ? sqlSelectPreview(s) : "\u2026";
    const rt = sqlResultType(s.aggregate, s.column_type);
    const ok4 = rt === element.data_type;
    typeHint.textContent = `Ergebnistyp ${typeName(rt)} ${ok4 ? "\u2713 passt zu" : "\u2717 passt nicht zu"} \u201E${element.name}\u201C (${typeName(element.data_type)})`;
    typeHint.className = "sub " + (ok4 ? "ok-hint" : "bad-hint");
    let ok6 = true, msg = "";
    if (s.cardinality === "KEY_UNIQUE") {
      ok6 = !!s.unique_column && s.filters.some((f) => f.operator === "EQ" && f.column === s.unique_column);
      msg = ok6 ? "H\u00F6chstens eine Zeile (eindeutiger Schl\u00FCssel)" : "Gleichheitsfilter auf die eindeutige Spalte n\u00F6tig";
    } else if (s.cardinality === "AGGREGATE") {
      ok6 = s.aggregate !== "NONE";
      msg = ok6 ? "Aggregat liefert genau eine Zeile" : "Aggregat w\u00E4hlen";
    } else {
      ok6 = s.order_by.length > 0;
      msg = ok6 ? "Erste Zeile nach Sortierung" : "Sortierspalte angeben";
    }
    cardHint.textContent = (ok6 ? "\u2713 " : "\u2717 ") + msg;
    cardHint.className = "sub " + (ok6 ? "ok-hint" : "bad-hint");
    uniqueRow.style.display = s.cardinality === "KEY_UNIQUE" ? "" : "none";
    orderRow.style.display = s.cardinality === "FIRST_ORDERED" ? "" : "none";
  }

  function buildFilterRow(f) {
    const col = el("input", { type: "text", placeholder: "DB-Spalte", list: "pw-sql-cols", value: f.column });
    col.addEventListener("input", () => { f.column = col.value; refresh(); });
    const opSel = el("select", null, ...SQL_OPERATORS.map(([v, l]) => el("option", { value: v }, l)));
    opSel.value = f.operator;
    opSel.addEventListener("change", () => { f.operator = opSel.value; refresh(); });
    const srcSel = el("select", null, ...instanceElems.map((d) => el("option", { value: d.id }, `${d.name} (${typeName(d.data_type)})`)));
    srcSel.value = f.source;
    srcSel.addEventListener("change", () => { f.source = srcSel.value; refresh(); });
    const rm = el("button", { class: "btn small danger", type: "button", onClick: () => { const i = filters.indexOf(f); if (i >= 0) filters.splice(i, 1); renderFilters(); refresh(); } }, "\u00D7");
    return el("div", { class: "check-row" }, col, opSel, srcSel, rm);
  }
  function renderFilters() { clear(filtersBox); filters.forEach((f) => filtersBox.appendChild(buildFilterRow(f))); }
  function addFilter() {
    filters.push({ column: "", operator: "EQ", source: instanceElems.length ? instanceElems[0].id : "" });
    renderFilters(); refresh();
  }

  conn.addEventListener("change", refresh);
  entity.addEventListener("input", refresh);
  colInput.addEventListener("input", () => { applyColType(); refresh(); });
  colType.addEventListener("change", refresh);
  agg.addEventListener("change", refresh);
  card.addEventListener("change", refresh);
  uniqueCol.addEventListener("input", refresh);
  orderCol.addEventListener("input", refresh);
  orderDesc.addEventListener("change", refresh);

  const uniqueRow = el("label", { class: "field" }, "Eindeutige Spalte (Schl\u00FCssel)", uniqueCol);
  const orderRow = el("label", { class: "field" }, "Sortierung",
    el("div", { class: "check-row" }, orderCol, el("label", { class: "check-inline" }, orderDesc, " absteigend")));
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Connector", conns.length ? conn : el("span", { class: "muted" }, "Erst einen Connector registrieren.")),
    el("label", { class: "field wide" }, "Entit\u00E4t/Tabelle", el("div", { class: "check-row" }, entity, el("button", { class: "btn small", type: "button", onClick: loadColumns }, "Spalten laden")), entityList),
    el("label", { class: "field" }, "Ergebnis-Spalte", colInput),
    el("label", { class: "field" }, "Spaltentyp", colType),
    el("label", { class: "field" }, "Aggregat", agg),
    el("label", { class: "field" }, "Kardinalit\u00E4t", card),
    uniqueRow, orderRow,
    el("div", { class: "sub-h" }, el("h3", null, "Filter (WHERE)"), el("span", { style: "flex:1" }), el("button", { class: "btn small", type: "button", onClick: addFilter }, "+ Filter")),
    filtersBox,
    el("div", { class: "sub-h" }, el("h3", null, "Vorschau")),
    typeHint, cardHint, preview, colDatalist);

  openModal(`SQL-Select \u2013 ${element.name}`, body, async () => {
    const s = spec();
    if (!s.connector_id || !s.entity || !s.column) { toast("info", "Connector, Entit\u00E4t und Ergebnis-Spalte angeben"); return false; }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${element.id}/sql-select`, s), "Datenelement per SQL-Select angebunden");
  }, "Anbinden");
  renderFilters(); refresh();
}

// --- 11.2c SQL-Write-Assistent (C7-C9) ------------------------------------

// Client-side mirror of compile_update for the live preview (server-authoritative).
function sqlUpdatePreview(s) {
  let sql = `UPDATE ${s.entity.split(".").map(sqlQuoteIdent).join(".")} SET ${sqlQuoteIdent(s.column)} = :val`;
  const where = s.filters.map((f, i) => `${sqlQuoteIdent(f.column)} ${sqlOpText(f.operator)} :f${i}`);
  if (where.length) sql += " WHERE " + where.join(" AND ");
  return sql;
}

function bindSqlWrite(element) {
  const schema = state.schema;
  const conns = Object.values(schema.connectors || {});
  const instanceElems = Object.values(schema.data_elements).filter((d) => d.source !== "EXTERNAL" && d.id !== element.id);
  const sourceType = (id) => { const e = schema.data_elements[id]; return e ? e.data_type : null; };

  const conn = el("select", null, ...conns.map((c) => el("option", { value: c.id }, `${c.name} (${c.id})`)));
  const entity = el("input", { type: "text", placeholder: "z. B. Kunde" });
  const entityList = wireEntitySuggestions(entity, "pw-sqlw-entities", () => conn.value, conn);
  const colInput = el("input", { type: "text", placeholder: "z. B. status", list: "pw-sqlw-cols" });
  const colDatalist = el("datalist", { id: "pw-sqlw-cols" });
  const colType = el("select", null, ...DATA_TYPES.map((t) => el("option", { value: t }, typeName(t))));
  colType.value = element.data_type;
  const uniqueCol = el("input", { type: "text", placeholder: "z. B. kd_id" });
  const preview = el("pre", { class: "code-block" });
  const typeHint = el("div", { class: "sub" });
  const cardHint = el("div", { class: "sub" });
  const filtersBox = el("div", null);
  let columns = null;
  const filters = [];

  function applyColType() {
    if (!columns) return;
    const hit = columns.find((c) => c.column === colInput.value.trim());
    if (hit && hit.data_type) colType.value = hit.data_type;
  }
  async function loadColumns() {
    if (!conn.value || !entity.value.trim()) { toast("info", "Erst Connector und Entit\u00E4t w\u00E4hlen"); return; }
    try {
      columns = await api.get(`/v1/connectors/${conn.value}/columns?entity=${encodeURIComponent(entity.value.trim())}`);
      clear(colDatalist);
      columns.forEach((c) => colDatalist.appendChild(el("option", { value: c.column }, `${c.sql_type} \u2192 ${typeName(c.data_type)}`)));
      applyColType(); refresh();
      toast("ok", `${columns.length} Spalten geladen`);
    } catch (err) { columns = null; const d = describeError(err); toast("info", "Keine Live-Spalten \u2013 Namen/Typ manuell", d.lines); }
  }

  function spec() {
    return {
      connector_id: conn.value,
      entity: entity.value.trim(),
      column: colInput.value.trim(),
      column_type: colType.value,
      filters: filters.filter((f) => f.column.trim() && f.source)
        .map((f) => ({ column: f.column.trim(), column_type: sourceType(f.source), operator: f.operator, key_element_id: f.source })),
      unique_column: uniqueCol.value.trim(),
    };
  }
  function refresh() {
    const s = spec();
    preview.textContent = (s.column && s.entity) ? sqlUpdatePreview(s) : "\u2026";
    const ok7 = s.column_type === element.data_type;
    typeHint.textContent = `Zielspalte ${typeName(s.column_type)} ${ok7 ? "\u2713 passt zu" : "\u2717 passt nicht zu"} \u201E${element.name}\u201C (${typeName(element.data_type)})`;
    typeHint.className = "sub " + (ok7 ? "ok-hint" : "bad-hint");
    const ok9 = !!s.unique_column && s.filters.some((f) => f.operator === "EQ" && f.column === s.unique_column);
    cardHint.textContent = (ok9 ? "\u2713 " : "\u2717 ") + (ok9 ? "Trifft genau eine Zeile (eindeutiger Schl\u00FCssel)" : "Gleichheitsfilter auf die eindeutige Spalte n\u00F6tig");
    cardHint.className = "sub " + (ok9 ? "ok-hint" : "bad-hint");
  }

  function buildFilterRow(f) {
    const col = el("input", { type: "text", placeholder: "DB-Spalte", list: "pw-sqlw-cols", value: f.column });
    col.addEventListener("input", () => { f.column = col.value; refresh(); });
    const opSel = el("select", null, ...SQL_OPERATORS.map(([v, l]) => el("option", { value: v }, l)));
    opSel.value = f.operator;
    opSel.addEventListener("change", () => { f.operator = opSel.value; refresh(); });
    const srcSel = el("select", null, ...instanceElems.map((d) => el("option", { value: d.id }, `${d.name} (${typeName(d.data_type)})`)));
    srcSel.value = f.source;
    srcSel.addEventListener("change", () => { f.source = srcSel.value; refresh(); });
    const rm = el("button", { class: "btn small danger", type: "button", onClick: () => { const i = filters.indexOf(f); if (i >= 0) filters.splice(i, 1); renderFilters(); refresh(); } }, "\u00D7");
    return el("div", { class: "check-row" }, col, opSel, srcSel, rm);
  }
  function renderFilters() { clear(filtersBox); filters.forEach((f) => filtersBox.appendChild(buildFilterRow(f))); }
  function addFilter() {
    filters.push({ column: "", operator: "EQ", source: instanceElems.length ? instanceElems[0].id : "" });
    renderFilters(); refresh();
  }

  conn.addEventListener("change", refresh);
  entity.addEventListener("input", refresh);
  colInput.addEventListener("input", () => { applyColType(); refresh(); });
  colType.addEventListener("change", refresh);
  uniqueCol.addEventListener("input", refresh);

  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Connector", conns.length ? conn : el("span", { class: "muted" }, "Erst einen Connector registrieren.")),
    el("label", { class: "field wide" }, "Entit\u00E4t/Tabelle", el("div", { class: "check-row" }, entity, el("button", { class: "btn small", type: "button", onClick: loadColumns }, "Spalten laden")), entityList),
    el("label", { class: "field" }, "Ziel-Spalte", colInput),
    el("label", { class: "field" }, "Spaltentyp", colType),
    el("label", { class: "field" }, "Eindeutige Spalte (Schl\u00FCssel)", uniqueCol),
    el("div", { class: "sub-h" }, el("h3", null, "Filter (WHERE)"), el("span", { style: "flex:1" }), el("button", { class: "btn small", type: "button", onClick: addFilter }, "+ Filter")),
    filtersBox,
    el("div", { class: "sub-h" }, el("h3", null, "Vorschau")),
    typeHint, cardHint, preview, colDatalist);

  openModal(`SQL-Write \u2013 ${element.name}`, body, async () => {
    const s = spec();
    if (!s.connector_id || !s.entity || !s.column) { toast("info", "Connector, Entit\u00E4t und Ziel-Spalte angeben"); return false; }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/data-elements/${element.id}/sql-write`, s), "Datenelement per SQL-Write angebunden");
  }, "Anbinden");
  renderFilters(); refresh();
}

// --- 11.3 Automatik-Schritt-Binding ---------------------------------------

function automationPanel() {
  const head = el("div", { class: "panel-h" },
    el("h2", null, "Automatik-Schritte"),
    el("span", { class: "sub" }, "Person / Automatisch (External-Task \u00B7 HTTP-Push)"));
  if (!state.schema) {
    return el("div", { class: "panel" }, head,
      el("div", { class: "panel-b" }, emptyState("Kein Schema ausgew\u00E4hlt.")));
  }
  const schema = state.schema;
  const draft = isDraft(schema);
  const acts = activitiesOf(schema);
  const rows = acts.map((n) => {
    const sb = (schema.service_bindings || {})[n.id];
    const auto = sb ? sb.automation : "MANUAL_NONE";
    const detail = auto === "EXTERNAL_TASK" ? `External-Task \u00B7 Topic ${sb.topic || "?"}`
      : auto === "HTTP_PUSH" ? `HTTP-Push \u00B7 ${sb.endpoint_ref || "?"}`
      : "Person / manuell";
    const pill = el("span", { class: "pill " + (auto === "MANUAL_NONE" ? "pill-gray" : "pill-green") },
      auto === "MANUAL_NONE" ? "manuell" : "automatisch");
    const btn = sb
      ? el("button", { class: "btn small", onClick: () => editAutomation(n, sb), ...lockedBy([!draft, DRAFT_ONLY_REASON]) }, "Bearbeitung w\u00E4hlen")
      : el("span", { class: "muted" }, "erst Dienst zuweisen");
    return [nodeCaption(n), pill, detail, btn];
  });
  const body = el("div", { class: "panel-b" },
    acts.length ? table(["Aktivit\u00E4t", "Modus", "Anbindung", ""], rows)
      : emptyState("Keine Aktivit\u00E4ten im Schema."));
  return el("div", { class: "panel" }, head, body);
}

function editAutomation(node, sb) {
  const kindSel = el("select", null,
    el("option", { value: "MANUAL_NONE" }, "Person / manuell"),
    el("option", { value: "EXTERNAL_TASK" }, "Automatisch \u00B7 External-Task (Topic)"),
    el("option", { value: "HTTP_PUSH" }, "Automatisch \u00B7 HTTP-Push (Ziel)"));
  kindSel.value = sb.automation || "MANUAL_NONE";
  const topic = el("input", { type: "text", placeholder: "z. B. invoice-check", value: sb.topic || "" });
  const endpoint = el("input", { type: "text", placeholder: "z. B. webhook_1", value: sb.endpoint_ref || "" });
  const retryMax = el("input", { type: "number", min: "0", value: String(sb.retry_max != null ? sb.retry_max : 5) });
  const backoff = el("input", { type: "number", min: "0", value: String(sb.retry_backoff_ms != null ? sb.retry_backoff_ms : 2000) });
  const timeout = el("input", { type: "number", min: "0", value: String(sb.request_timeout_ms != null ? sb.request_timeout_ms : 30000) });
  const topicField = el("label", { class: "field" }, "Topic", topic);
  const endpointField = el("label", { class: "field" }, "Endpunkt-Referenz", endpoint);
  const advanced = el("details", { class: "adv-block" }, el("summary", null, "Erweitert (Robustheit)"),
    el("div", { class: "form-grid" },
      el("label", { class: "field" }, "Max. Versuche", retryMax),
      el("label", { class: "field" }, "Backoff (ms)", backoff),
      el("label", { class: "field" }, "Timeout (ms)", timeout)));
  const syncFields = () => {
    const k = kindSel.value;
    topicField.style.display = k === "EXTERNAL_TASK" ? "" : "none";
    endpointField.style.display = k === "HTTP_PUSH" ? "" : "none";
    advanced.style.display = k === "MANUAL_NONE" ? "none" : "";
  };
  kindSel.addEventListener("change", syncFields);
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field" }, "Bearbeitung", kindSel), topicField, endpointField, advanced);
  syncFields();
  openModal(`Automatik \u2013 ${nodeCaption(node)}`, body, async () => {
    const k = kindSel.value;
    const req = { node_id: node.id, automation: k };
    if (k === "EXTERNAL_TASK") { if (!topic.value.trim()) return false; req.topic = topic.value.trim(); }
    if (k === "HTTP_PUSH") { if (!endpoint.value.trim()) return false; req.endpoint_ref = endpoint.value.trim(); }
    if (k !== "MANUAL_NONE") {
      req.retry_max = Number(retryMax.value);
      req.retry_backoff_ms = Number(backoff.value);
      req.request_timeout_ms = Number(timeout.value);
    }
    return commitSchemaChange(() => api.post(`/schemas/${state.schemaId}/automation`, req), "Automatik gesetzt");
  }, "\u00DCbernehmen");
}

// --- 11.4 Webhook-/Ereignis-Panel -----------------------------------------

async function webhookPanel() {
  let subs = [];
  try { subs = await api.get("/v1/webhooks"); }
  catch (err) { toastError(err); }
  const rows = subs.map((s) => {
    const events = (s.events || []).map(webhookEventLabel).join(", ");
    const test = el("button", { class: "btn small", onClick: () => testWebhook(s.id) }, "Testzustellung");
    const log = el("button", { class: "btn small ghost", onClick: () => showDeliveries(s.id) }, "Protokoll");
    const del = el("button", { class: "btn small danger", onClick: () => deleteWebhook(s) }, "L\u00F6schen");
    return [s.url, events, s.secret_ref || "\u2013", el("div", { class: "row-actions" }, test, log, del)];
  });
  const addBtn = el("button", { class: "btn small", onClick: addWebhook }, "+ Abonnement");
  const body = el("div", { class: "panel-b" },
    // In der oeffentlichen Demo ist jede ausgehende Verbindung gesperrt
    // (PROCWORKS_EGRESS_DENY). Ohne Hinweis wirkt das wie ein Defekt; der
    // Probelauf zeigt trotzdem, was gesendet wuerde und wie die SSRF-Regel urteilt.
    state.demoPassword
      ? el("div", { class: "warn-banner", style: "margin-bottom:10px" },
          "In der öffentlichen Demo sind ausgehende Verbindungen gesperrt – Zustellungen scheitern hier absichtlich. Beim Anlegen zeigt „Probelauf“, was gesendet würde und ob ein Ziel die Sicherheitsprüfung besteht (interne Adressen werden abgelehnt).")
      : null,
    subs.length ? table(["Ziel-URL", "Ereignisse", "Secret-Ref", ""], rows)
      : emptyState("Noch keine Webhook-Abonnements."));
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Webhooks / Ereignisse"), el("span", { class: "spacer", style: "flex:1" }), addBtn),
    body);
}

function addWebhook() {
  // Auflösbares Beispiel: „hooks.example.com" existiert nicht, der eigene
  // Vorschlag scheiterte deshalb an der Zielprüfung.
  const url = el("input", { type: "url", placeholder: "https://example.com/procworks" });
  const secret = el("input", { type: "text", placeholder: "z. B. WEBHOOK_SECRET (optional)" });
  const checks = WEBHOOK_EVENT_TYPES.map((ev) => {
    const cb = el("input", { type: "checkbox", value: ev });
    return { cb, row: el("label", { class: "check-row" }, cb, " " + webhookEventLabel(ev)) };
  });
  const result = el("div", { class: "wh-preview" });
  // Ein Probelauf gilt nur fuer die Eingaben, mit denen er lief. Aendert sich
  // URL, Secret oder Ereignis, verschwindet das Ergebnis -- sonst stand der
  // Befund zu „127.0.0.1“ noch unter einer laengst anderen URL. ``previewGen``
  // verwirft zudem eine Antwort, die erst nach einer Aenderung eintrifft.
  let previewGen = 0;
  const dropPreview = () => { previewGen++; clear(result); };
  url.addEventListener("input", dropPreview);
  secret.addEventListener("input", dropPreview);
  checks.forEach((c) => c.cb.addEventListener("change", dropPreview));
  const previewBtn = el("button", { class: "btn small", onClick: async () => {
    const chosen = checks.filter((c) => c.cb.checked).map((c) => c.cb.value);
    if (!url.value.trim()) { toast("info", "Bitte eine Ziel-URL angeben"); return; }
    const gen = ++previewGen;
    try {
      const p = await api.post("/v1/webhooks/preview",
        { url: url.value.trim(), event: chosen[0] || "task.completed", secret_ref: secret.value.trim() });
      if (gen === previewGen) renderWebhookPreview(result, p);
    } catch (err) { if (gen === previewGen) toastError(err); }
  } }, "Probelauf (sendet nichts)");
  // Jedes Feld in eigener Zeile: Die URL ist lang, und die Ereignisliste
  // drueckte im Dreier-Raster URL und Secret auf ein Drittel zusammen.
  const body = el("div", { class: "form-grid" },
    el("label", { class: "field wide" }, "Ziel-URL", url),
    el("label", { class: "field wide" }, "Secret-Referenz (Servername, optional)", secret),
    el("div", { class: "field wide" }, el("span", null, "Ereignisse"),
      el("div", { class: "check-list" }, ...checks.map((c) => c.row))),
    el("div", null, previewBtn), result);
  openModal("Webhook-Abonnement", body, async () => {
    const events = checks.filter((c) => c.cb.checked).map((c) => c.cb.value);
    if (!url.value.trim() || !events.length) return false;
    try {
      await api.post("/v1/webhooks", { url: url.value.trim(), events, secret_ref: secret.value.trim() });
      render(); toast("ok", "Webhook angelegt");
    } catch (err) { toastError(err); return false; }
  }, "Anlegen");
}

/**
 * Ergebnis eines Webhook-Probelaufs (``POST /v1/webhooks/preview``) anzeigen.
 *
 * Der Kern sendet dabei nichts. Gezeigt werden das Urteil der SSRF-Regel (mit
 * derselben Begruendung, die eine echte Zustellung haette), eine Egress-Sperre
 * getrennt davon, und Kopfzeilen samt signiertem Rumpf -- ein Empfaenger kann
 * die Signatur daran nachpruefen.
 *
 * @param {HTMLElement} box Zielbehaelter (wird geleert)
 * @param {object} p Antwort des Probelaufs
 */
function renderWebhookPreview(box, p) {
  clear(box);
  box.appendChild(p.allowed
    ? el("div", { class: "ok-banner" }, `\u2713 Ziel zulässig${p.resolved_address ? ` (Adresse ${p.resolved_address})` : ""}.`)
    : el("div", { class: "warn-banner" }, "Ziel abgelehnt: " + findingText(
        { code: p.reason_code, params: p.reason_params, message: p.reason }, { withHint: true })));
  if (p.egress_locked) {
    box.appendChild(el("div", { class: "warn-banner", style: "margin-top:6px" },
      "Auf dieser Instanz sind ausgehende Verbindungen gesperrt – gesendet würde trotzdem nichts."));
  }
  box.appendChild(el("div", { class: "muted", style: "font-size:12px;margin-top:8px" },
    p.signed
      ? "Signiert (X-ProcWorks-Signature, HMAC-SHA256 über den Rumpf)."
      : !p.secret_ref
        ? "Nicht signiert – es wurde keine Secret-Referenz angegeben."
        : `Nicht signiert – die Secret-Referenz „${p.secret_ref}“ ist auf diesem Server nicht hinterlegt. Das Secret selbst wird serverseitig gesetzt und verlässt ihn nie.`));
  const headerText = Object.entries(p.headers).map(([k, v]) => `${k}: ${v}`).join("\n");
  box.appendChild(el("pre", { class: "wh-preview-code" }, headerText + "\n\n" + p.body));
}

async function testWebhook(id) {
  try {
    const d = await api.post(`/v1/webhooks/${id}/test`);
    const detail = d.status_code != null ? "HTTP " + d.status_code : (d.error || "");
    toast(d.ok ? "ok" : "err", d.ok ? "Testzustellung erfolgreich" : "Testzustellung fehlgeschlagen", detail ? [detail] : []);
  } catch (err) { toastError(err); }
}

async function showDeliveries(id) {
  let deliveries = [];
  try { deliveries = await api.get(`/v1/webhooks/${id}/deliveries`); }
  catch (err) { toastError(err); return; }
  const rows = deliveries.map((d) => [
    fmtTimestamp(new Date(d.at * 1000).toISOString()),
    d.event_type, String(d.attempt),
    d.ok ? el("span", { class: "pill pill-green" }, "ok") : el("span", { class: "pill pill-red" }, "Fehler"),
    d.status_code != null ? "HTTP " + d.status_code : (d.error || "\u2013"),
  ]);
  const body = rows.length ? table(["Zeit", "Ereignis", "Versuch", "Status", "Detail"], rows)
    : emptyState("Noch keine Zustellungen.");
  openModal("Zustellprotokoll", body, async () => true, "Schlie\u00DFen");
}

function deleteWebhook(sub) {
  openModal("Webhook l\u00F6schen", el("p", { class: "muted" }, `Abonnement f\u00FCr ${sub.url} entfernen?`), async () => {
    try { await api.del(`/v1/webhooks/${sub.id}`); render(); toast("ok", "Webhook gel\u00F6scht"); }
    catch (err) { toastError(err); return false; }
  }, "L\u00F6schen", { danger: true });
}

// --------------------------------------------------------------------------
// Wiederverwendbare UI-Bausteine
// --------------------------------------------------------------------------

function emptyState(text) { return el("div", { class: "empty" }, text); }
function kpi(label, value) { return el("div", { class: "kpi" }, el("div", { class: "label" }, label), el("div", { class: "value" }, String(value))); }

/**
 * Baut eine Standardtabelle aus Kopfzeilen und Zeilen.
 *
 * Jede Zelle erhaelt zusaetzlich ein `data-label`-Attribut mit dem Text ihrer
 * Spaltenueberschrift. Auf dem Desktop ist das Attribut wirkungslos; in der
 * mobilen Ansicht (styles.css, Breakpoint <= 720px) rendert die Tabelle als
 * gestapelte Karten und blendet die Kopfzeile aus – dort dient `data-label`
 * als vorangestellte Feldbeschriftung, sodass jede Zeile (z. B. eine Aufgabe)
 * ohne horizontales Scrollen lesbar bleibt. Leere Ueberschriften (etwa die
 * Aktionsspalte mit dem „Erledigen"-Knopf) tragen bewusst kein Label, damit die
 * Zelle mobil die volle Breite nutzt.
 *
 * Alle Tabellen laufen hierueber: Tabellen, die ``el("table")`` direkt bauten
 * (Instanzliste, Eskalationen, Zustaendigkeit, Migration), zeigten mobil
 * Karten ohne „Instanz/Schema/Status“ (Waechter
 * ``test_every_table_is_built_by_the_shared_helper``).
 *
 * @param {string[]} headers Spaltenueberschriften (leere Strings erlaubt).
 * @param {Array<Array>} rows Zeilen; jede ein Array von Zellen (Node oder Text).
 * @param {(i:number)=>(string|object|null)} [rowAttrs] je Zeilenindex eine
 *   CSS-Klasse (String) oder Attribute (Objekt, z. B. ``{class, onClick}``).
 * @param {object} [tableAttrs] Attribute der Tabelle (z. B. ``{class}``).
 * @returns {Node} Das <table>-Element.
 */
function table(headers, rows, rowAttrs, tableAttrs) {
  const attrsOf = (i) => {
    const a = rowAttrs ? rowAttrs(i) : null;
    if (a == null || a === "") return null;
    return typeof a === "string" ? { class: a } : a;
  };
  return el("table", tableAttrs || null,
    el("thead", null, el("tr", null, ...headers.map((h) => el("th", null, h)))),
    el("tbody", null, ...rows.map((r, i) => el("tr", attrsOf(i),
      ...r.map((c, j) => {
        const label = headers[j];
        const attrs = label ? { "data-label": String(label) } : null;
        return el("td", attrs, c instanceof Node ? c : String(c));
      })))));
}

// --------------------------------------------------------------------------
// View: Hilfe (kontextsensitive In-App-Hilfe + Glossar der Regel-Codes)
// --------------------------------------------------------------------------

// Public documentation targets. The source repository is private, so links must
// not point at it: the modeller guide is published on the website (procworks.de),
// customer guides live in the public release repo (procworks-release). docUrl()
// resolves a doc filename to the right public URL for the help view.
const SITE_DOCS = "https://procworks.de/docs/";
const RELEASE_DOCS = "https://github.com/tobiasHaecker/procworks-release/blob/main/docs/";
const DISCLAIMER_URL = "https://github.com/tobiasHaecker/procworks-release/blob/main/DISCLAIMER.md";
const DOC_URLS = {
  "Modellierer-Anleitung.md": SITE_DOCS + "modellierer-anleitung.html",
  // Verlinkt werden nur Dokumente, die öffentlich bereitstehen (Website oder
  // Kunden-Repo).
  "README.md": SITE_DOCS,
  "Mitarbeiter-Anleitung.md": RELEASE_DOCS + "Mitarbeiter-Anleitung.md",
  "Windows-Server-Setup.md": RELEASE_DOCS + "Windows-Server-Setup.md",
  "Integrations-Leitfaden.md": RELEASE_DOCS + "Integrations-Leitfaden.md",
  "Betriebs-Backup-Leitfaden.md": RELEASE_DOCS + "Betriebs-Backup-Leitfaden.md",
};
// Resolve a documentation filename to its public URL (falls back to the website).
function docUrl(doc) { return DOC_URLS[doc] || (SITE_DOCS + doc); }

// Short purpose of each navigation view (mirrors VIEW_META plus a one-liner of
// what the user actually does there).
const HELP_VIEWS = [
  ["\u25A3 Modellieren", "Schritte \u00FCber \u201E+\u201C einf\u00FCgen (seriell / parallel / bedingt), umbenennen, entfernen, Befunde pr\u00FCfen, freigeben."],
  ["\u2630 Datensicht", "Datenelemente anlegen, bearbeiten/l\u00F6schen und je Aktivit\u00E4t Lesen/Schreiben verbinden (Datenfluss D)."],
  ["\u265F Ressourcensicht", "Organisation (Rollen, Einheiten, Agenten) und Bearbeiterregeln je Schritt (Z/A)."],
  ["\u25B6 Ausf\u00FChrung", "Freigegebene Prozesse starten und die Schritte eines Vorgangs abarbeiten. Verzweigungen entscheidet das System anhand der erfassten Daten. Modellierer starten Entw\u00FCrfe als Test-Instanz."],
  ["\u2630 Meine Aufgaben", "Pers\u00F6nliche Arbeitsliste \u2013 Aufgaben mit \u201EErledigen\u201C abschlie\u00DFen."],
  ["\u2697 Pr\u00FCfinstanz", "Nur Modellierer und Administratoren: einen Entwurf als Test-Instanz durchspielen \u2013 alle Aufgaben an einer Stelle, ohne Mails und Eskalationen."],
  ["\u2609 Monitoring", "Live-Status aktiver Instanzen, Prozesslandkarte, Inzidente externer Aufgaben."],
  ["\u21C4 Integration", "Connectoren, externe Datenbindung, Automatik (External-Task / HTTP-Push), Webhooks."],
  ["\u2699 Administration", "Nur f\u00FCr Administratoren: Benutzer, Datensicherung, E-Mail-Ausgang, Wartung (Zur\u00FCcksetzen) und Beispieldaten."],
  ["\u24D8 Hilfe", "Diese Seite: Sichten, Schnellstart je Rolle, Glossar der Regel-Codes und die gef\u00FChrten Touren."],
];

// Role-oriented quick starts: each entry points at the matching how-to doc.
const HELP_QUICKSTART = [
  ["Modellierer", "Prozess erstellen, Daten/Bearbeiter verdrahten, testen, freigeben.", "Modellierer-Anleitung.md"],
  ["Sachbearbeiter", "Anmelden, eigene Aufgaben sehen und erledigen.", "Mitarbeiter-Anleitung.md"],
  ["Administrator", "Installation, Logins, Betrieb (Update/Backup), Beispieldaten.", "Windows-Server-Setup.md"],
  ["Integrator", "Fremdsysteme \u00FCber die offene /v1-Schnittstelle anbinden.", "Integrations-Leitfaden.md"],
];

// Glossary of the correctness rule codes that surface in the findings list and
// error toasts. Grouped by family so a user can look up exactly what e.g. "D1"
// or "B2" means. Kept in sync with validator.py.
const HELP_RULES = [
  ["Struktur & Kontrollfluss (K)", [
    ["K1", "Blockstruktur: jeder Split hat genau einen passenden Join desselben Typs."],
    ["K2", "Genau ein START und ein END; jede Aktivit\u00E4t hat genau eine Ein- und Ausgangskante."],
    ["K3", "Erreichbarkeit: kein isolierter Knoten, keine Sackgasse \u2013 alles liegt auf START\u2192END."],
    ["K4", "Sync-Kanten nur zwischen Aktivit\u00E4ten verschiedener UND-Zweige."],
    ["K5", "Soundness: jeder Zustand kann ordentlich zum Ende gelangen."],
    ["K6", "Strukturierte Schleifen (REPEAT-UNTIL mit definierter Abbruchbedingung)."],
    ["K7", "XOR-Pr\u00E4dikate decken den Wertebereich vollst\u00E4ndig und \u00FCberlappungsfrei ab."],
  ]],
  ["Datenfluss (D)", [
    ["D1", "Pflicht-Eingaben sind auf ALLEN Pfaden vor dem Lesen geschrieben."],
    ["D2", "Keine konkurrierenden Schreibzugriffe paralleler Zweige auf dasselbe Element."],
    ["D3", "Typkonformit\u00E4t von Quelle und Senke."],
    ["D4", "Optionale Eingaben d\u00FCrfen unversorgt bleiben; Join-Knoten tragen keine Daten."],
    ["D5", "Datenfluss wird live gepr\u00FCft und muss vor Freigabe sauber sein."],
    ["D6", "Beim Abschlie\u00DFen eines Schritts nur Werte, die dieser Schritt schreibt \u2013 nichts, was er nur liest, und keine unbekannten Felder."],
  ]],
  ["Externe Datenbindung (C)", [
    ["C1", "EXTERNE Elemente brauchen eine g\u00FCltige Connector-Bindung; INSTANCE-Elemente keine."],
    ["C2", "Das Schl\u00FCsselelement der Bindung ist ein existierendes INSTANCE-Element (nicht es selbst)."],
    ["C3", "Der gebundene Entit\u00E4tsname ist nicht leer."],
    ["C4\u2013C6", "SQL-Abfrage: Ergebnistyp passt zum Element, Filter passen zu ihren Spalten und Quellen, die Anzahl gelesener Zeilen ist eindeutig (eine Zeile, Zusammenfassung, erste nach Sortierung)."],
    ["C7\u2013C9", "Zur\u00FCckschreiben: Zielspalte passt zum Typ, Filter passen, genau eine Zeile wird getroffen (eindeutige Spalte)."],
  ]],
  ["Eingabemasken & Anzeige (U)", [
    ["U1", "Die Maske h\u00E4ngt an einem Aufgaben-Schritt und nennt existierende Datenelemente."],
    ["U2", "Felder sind stimmig: eindeutig, beschriftet, Bedienelement passt zum Typ, Pr\u00FCfregeln passen zum Feld."],
    ["U3", "Jedes Feld hat die passende Daten\u00ADbindung am Schritt (Eingabe schreibt, Anzeige liest)."],
    ["U4", "Beim Abschlie\u00DFen: Werte halten die Pr\u00FCfregeln der Maske ein (Grenzen, Muster, L\u00E4nge)."],
    ["U5", "Den Vorgang benennen h\u00F6chstens zwei vorhandene Vorgangsdaten."],
    ["U6", "Bezeichnungen sind h\u00F6chstens 200 Zeichen lang."],
  ]],
  ["Bearbeiter / Ressourcen (Z, A)", [
    ["Z1", "Bearbeiterregel ist syntaktisch g\u00FCltig und referenziert existierende Rollen/Einheiten."],
    ["Z2", "Die Regel ist erf\u00FCllbar \u2013 sie liefert mindestens einen Agenten."],
    ["Z3", "NodePerformingAgent(\u2026) verweist nur auf garantiert vorher laufende Schritte."],
    ["Z4", "Interaktive Schritte brauchen eine Bearbeiterregel; automatische nicht."],
    ["A1\u2013A3", "Zugeordneter Dienst (Template) ist vorhanden und typkonform an die Daten gebunden."],
  ]],
  ["Integration / Automatik (I)", [
    ["I1", "Automatik wohlgeformt: External-Task braucht Topic, HTTP-Push eine Endpunkt-Referenz."],
    ["I2", "Genau ein Automatik-Muster; automatisierte Bindung ist als automatisch markiert."],
    ["I3", "Parameter-Mapping zeigt auf existierende Datenelemente."],
    ["I4", "Topic/Endpunkt enthalten keine Inline-URL oder Zugangsdaten."],
    ["WH", "Webhook-Ziel: https, ein erlaubter \u00F6ffentlicher Server, keine Zugangsdaten in der Adresse."],
  ]],
  ["Komposition (H, F)", [
    ["H1\u2013H4", "Sub-Prozesse: nur freigegebene, gepinnte Version; typkonforme Schnittstelle; zyklenfrei."],
    ["F1\u2013F4", "Folgeprozesse: Ziel existiert freigegeben; typkonformes Handover; lose Kopplung bei ASYNC; Bedingungen lesen nur sicher gesetzte Daten."],
  ]],
  ["Zeit & Release (T, B)", [
    ["T1\u2013T2", "Fristen/Dauern wohldefiniert und entlang der Blockstruktur widerspruchsfrei."],
    ["T3", "Eskalation: nur an Aufgaben-Schritten mit Soll-Zeit, Stufen aufsteigend, Ziele finden jemanden."],
    ["B1", "Release-Reife: jeder Schritt ist ausf\u00FChrbar \u2013 automatische tragen einen Dienst, interaktive Maske und Bearbeiterzuordnung (der interaktive Teil ist B2)."],
    ["B2", "Release-Reife: jeder interaktive Schritt hat eine Bearbeiterzuordnung."],
    ["B3", "Release-Reife: alle Pflichtdaten sind gebunden, alle Pr\u00E4dikate spezifiziert."],
  ]],
  ["Benachrichtigung (N)", [
    ["N1", "E-Mail-Adressen und Postf\u00E4cher sind wohlgeformt."],
    ["N2", "Mails gibt es nur an Aufgaben-Schritten mit Bearbeiterzuordnung."],
    ["N3", "Jeder m\u00F6gliche Empf\u00E4nger ist erreichbar (Adresse bzw. Gruppenpostfach)."],
    ["N4", "Platzhalter im Mailtext verweisen auf sicher gesetzte Vorgangsdaten."],
  ]],
  ["Bearbeitungsschritte (OP)", [
    ["OP", "Vorbedingung einer Bearbeitung nicht erf\u00FCllt \u2013 etwa: das Element gibt es nicht (mehr), der Schritt liegt an der falschen Stelle, der Name fehlt."],
    ["USERS", "Benutzerverwaltung: Login schon vergeben, Rolle unbekannt, eigener oder letzter Administrator-Login nicht l\u00F6schbar."],
    ["PW", "Passwortregeln: Mindestl\u00E4nge, und das neue Passwort muss sich vom bisherigen unterscheiden."],
  ]],
  ["Laufzeit & Migration (R, M)", [
    ["R0", "Nur Entw\u00FCrfe sind editierbar; freigegebene Schemata sind unver\u00E4nderlich."],
    ["LC", "Lebenszyklus: nur ein Entwurf wird freigegeben, eine neue Revision entsteht nur aus einer freigegebenen Version."],
    ["M0", "Migration: die Instanz l\u00E4uft auf einer fr\u00FCheren Version genau dieses Schemas."],
    ["R1\u2013R2", "Ad-hoc-\u00C4nderungen nur zustandsvertr\u00E4glich und unter Erhalt aller K/D-Regeln."],
    ["M1\u2013M5", "Migration nur, wenn Ziel korrekt ist und der bisherige Verlauf vertr\u00E4glich bleibt."],
    ["EX", "Laufzeit: Die Aktion passt nicht zum Zustand des Vorgangs \u2013 etwa: jemand anderes hat den Schritt \u00FCbernommen, er ist angehalten oder gescheitert, oder man ist nicht zust\u00E4ndig."],
    ["BPMN", "BPMN-Import: Die Datei ist kein g\u00FCltiges BPMN oder enth\u00E4lt Elemente au\u00DFerhalb der unterst\u00FCtzten Block-Teilsprache (etwa ODER-Gateways oder Zwischenereignisse)."],
  ]],
  ["Modellhinweise (G, 7PMG)", [
    ["G1", "Hinweis: sehr gro\u00DFes Modell (>50 Knoten) \u2013 ggf. in Sub-Prozesse zerlegen."],
    ["G2", "Hinweis: hoher Gateway-Grad \u2013 Verzweigung vereinfachen."],
    ["G6", "Hinweis: hohe Verschachtelungstiefe (>5)."],
    ["G7", "Hinweis: Aktivit\u00E4t ohne sprechenden Namen."],
    ["G8", "Hinweis: interaktiver Schritt ohne Soll-Reaktionszeit, obwohl andere Schritte eine tragen \u2013 er nimmt nicht an der zeitbasierten Priorisierung teil."],
  ]],
];

function viewHelp() {
  const content = byId("content");
  clear(content);

  // Gefuehrte Tour -- ganz oben, weil sie der schnellste Einstieg ist. Zeigt je
  // Rolle des Angemeldeten eine Karte, inkl. Wiedereinstieg an der Stelle, an
  // der eine abgebrochene Tour stehengeblieben ist.
  content.appendChild(tourPanel());

  // Intro / principle.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "ProcWorks \u2013 Hilfe")),
    el("div", { class: "panel-b" },
      el("p", { class: "muted" },
        "ProcWorks h\u00E4lt jedes Modell \u201Ekorrekt per Konstruktion\u201C: Das Werkzeug ",
        "bietet nur Operationen an, die das Modell g\u00FCltig halten. Einen ",
        "\u201EValidieren\u201C-Knopf gibt es bewusst nicht \u2013 was noch zur Ausf\u00FChrbarkeit ",
        "fehlt, zeigt die Befunde-Liste laufend an."))));

  // The seven views.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Die Sichten im \u00DCberblick")),
    el("div", { class: "panel-b" },
      table(["Sicht", "Wof\u00FCr"], HELP_VIEWS.map(([n, d]) => [n, d])))));

  // Role-oriented quick starts with deep links to the how-to docs.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Schnellstart je Rolle")),
    el("div", { class: "panel-b" },
      table(["Rolle", "Ziel", "Anleitung"], HELP_QUICKSTART.map(([role, goal, doc]) => [
        role, goal,
        el("a", { href: docUrl(doc), target: "_blank", rel: "noopener" }, doc),
      ])))));

  // Glossary of rule codes, grouped by family.
  const glossary = el("div", { class: "panel-b" });
  for (const [group, rules] of HELP_RULES) {
    glossary.appendChild(el("div", { class: "sub-h" }, el("h3", null, group)));
    glossary.appendChild(table(["Code", "Bedeutung"], rules.map(([c, m]) => [
      el("span", { class: "pill pill-gray" }, c), m,
    ])));
  }
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Glossar der Regel-Codes"),
      el("span", { class: "spacer", style: "flex:1" }),
      el("span", { class: "muted", style: "font-size:12px" }, "erscheinen in Befunden & Fehlermeldungen")),
    glossary));

  // Further reading.
  content.appendChild(el("div", { class: "panel" },
    el("div", { class: "panel-h" }, el("h2", null, "Weiterf\u00FChrend")),
    el("div", { class: "panel-b" },
      el("ul", { style: "margin:4px 0;padding-left:18px;line-height:1.7" },
        el("li", null, el("a", { href: docUrl("README.md"), target: "_blank", rel: "noopener" }, "Dokumentations-\u00DCbersicht (nach Rolle)")),
        el("li", null, el("a", { href: DISCLAIMER_URL, target: "_blank", rel: "noopener" }, "Haftungsausschluss"))))));
}

/**
 * Baut das Panel „Gefuehrte Tour" der Hilfe-Sicht.
 *
 * Listet alle Touren, die zur Rolle des Angemeldeten passen. Eine abgebrochene
 * Tour bietet zusaetzlich das Fortsetzen an der gemerkten Stelle an; eine
 * bereits durchlaufene laesst sich jederzeit erneut starten -- die Hilfe ist
 * der dauerhafte Zugang, nachdem das automatische Angebot verstummt ist.
 *
 * @returns {HTMLElement} Das Panel (leer, falls keine Tour passt).
 */
function tourPanel() {
  if (typeof Tour === "undefined") return el("div");
  const tours = Tour.availableTours();
  if (!tours.length) return el("div");
  const cards = el("div", { class: "tour-cards" });
  tours.forEach((tour) => {
    const at = Tour.savedProgress(tour);
    cards.appendChild(el("div", { class: "tour-card" },
      el("b", null, tour.title),
      el("span", null,
        `${tour.subtitle} · ${tour.steps.length} Schritte` +
        (Tour.isDone(tour) ? " · bereits gesehen" : "")),
      el("div", { class: "row", style: "gap:6px" },
        at
          ? el("button", { class: "btn small primary", onClick: () => Tour.start(tour.id, { resume: true }) },
              `Bei Schritt ${at + 1} fortsetzen`)
          : null,
        el("button", { class: at ? "btn small ghost" : "btn small primary",
          onClick: () => Tour.start(tour.id, { resume: false }) },
          at ? "Von vorn" : "Tour starten"))));
  });
  return el("div", { class: "panel" },
    el("div", { class: "panel-h" },
      el("h2", null, "Geführte Tour"),
      el("span", { class: "sub" }, "Popups führen dich durch die wichtigsten Funktionen")),
    el("div", { class: "panel-b" },
      // Der Hinweis gilt nur der Modellierer-Tour (Sandkasten) -- ohne sie
      // nur der allgemeine Teil.
      el("p", { class: "muted", style: "font-size:13px;margin:0 0 12px" },
        tours.some((t) => t.sandbox)
          ? "Die Modellierer-Tour arbeitet auf einem Beispielprozess in deinem Browser – es wird dabei nichts gespeichert. "
          : "",
        "Abbrechen jederzeit mit Esc."),
      cards));
}

// --------------------------------------------------------------------------
// Navigation + Render-Dispatch
// --------------------------------------------------------------------------

const VIEW_META = {
  model: { title: "Modellieren", sub: "Gef\u00FChrte +-Operationen, live-validiert", fn: viewModel },
  data: { title: "Datensicht", sub: "Datenelemente + Lese/Schreib-Bindung (D/C)", fn: viewData },
  org: { title: "Ressourcensicht", sub: "Organisationsmodell + Bearbeiterregeln (Z/A)", fn: viewOrg },
  run: { title: "Ausf\u00FChrung", sub: "Instanzen starten und Arbeitsliste abarbeiten", fn: viewRun },
  tasks: { title: "Meine Aufgaben", sub: "Deine offenen Aufgaben \u00FCber alle Prozesse, inklusive Vertretung", fn: viewTasks },
  testrun: { title: "Pr\u00FCfinstanz", sub: "Test-Instanz eines Entwurfs im 4-Quadranten-Cockpit durchspielen", fn: viewTestRun },
  monitor: { title: "Monitoring", sub: "Live-Status aktiver Instanzen", fn: viewMonitor },
  integration: { title: "Integration", sub: "Connectoren, Datenanbindung, Automatik & Webhooks", fn: viewIntegration },
  admin: { title: "Administration", sub: "Betrieb: Datensicherung, Wartung & Beispieldaten", fn: viewAdmin },
  help: { title: "Hilfe", sub: "Sichten, Schnellstart je Rolle & Glossar der Regel-Codes", fn: viewHelp },
};

function setActiveNav() {
  [...byId("nav").children].forEach((b) => b.classList.toggle("active", b.dataset.view === state.view));
}

// --- Auth / Login ----------------------------------------------------------

// German labels for the coarse RBAC roles (technical ids stay English).
const ROLE_LABELS = { admin: "Administrator", modeler: "Modellierer", operator: "Bearbeiter", viewer: "Leser" };

// Which roles may see each navigation view. In open dev mode the principal
// holds every role, so the full UI stays visible exactly as before.
const VIEW_ROLES = {
  model: ["modeler", "admin"],
  data: ["modeler", "admin"],
  org: ["modeler", "admin"],
  run: ["operator", "modeler", "admin"],
  tasks: ["operator", "modeler", "admin"],
  testrun: ["modeler", "admin"],
  monitor: ["viewer", "operator", "modeler", "admin"],
  integration: ["modeler", "admin"],
  admin: ["admin"],
  help: ["viewer", "operator", "modeler", "admin"],
};

function currentRoles() {
  return (state.principal && state.principal.roles) || [];
}

function hasRole(...allowed) {
  const roles = currentRoles();
  return allowed.some((r) => roles.includes(r));
}

// Fetch the verified identity from the API (/auth/me). On 401 the token is
// invalid; we drop it and fall back to anonymous so the UI stays usable.
// Gehoeren die gemerkten Sitzungsdaten einem anderen Login (SESSION_OWNER_KEY)
// – Wechsel nach abgelaufener Sitzung, Neuladen nach fremder Anmeldung,
// Rollenwechsel der Demo –, werden sie verworfen (resetSessionState). Derselbe
// Login behaelt sie, damit ein Neuladen die Arbeit nicht verliert.
async function loadPrincipal() {
  const before = state.principal && state.principal.subject;
  try {
    state.principal = await api.get("/auth/me");
    // Anderer Login: Meldungen der vorigen Person gehoeren nicht hierher.
    if (before !== undefined && before !== (state.principal && state.principal.subject)) clearToasts();
    const subject = (state.principal && state.principal.subject) || "";
    const owner = storageGet(localStorage, SESSION_OWNER_KEY);
    if (owner !== subject) {
      // Kein Eintrag (null) ist ebenfalls fremd: Er fehlt nach dem Abmelden
      // (dann ist ohnehin alles leer) und bei Altbestand aus der Zeit vor der
      // Bindung, dessen Herkunft unbekannt ist.
      resetSessionState();
      storageSet(localStorage, SESSION_OWNER_KEY, subject);
    }
  } catch (err) {
    state.principal = null;
    // 401: Diese Anmeldung gilt nicht (Token geleert, ungueltig, abgelaufen).
    // Dann gehoert der gemerkte Stand niemandem mehr, der hier sitzt – sonst
    // zeichnete etwa die Ausfuehrung den Vorgang der vorigen Person weiter.
    // Ein Netzfehler (kein status) laesst ihn stehen: Die Person ist dieselbe.
    if (err && err.status === 401) {
      resetSessionState();
      try { localStorage.removeItem(SESSION_OWNER_KEY); } catch (_e) { /* nur Komfort */ }
    }
    if (err && err.status === 401 && state.token && !demoRecovering) {
      toast("err", "Anmeldung fehlgeschlagen", ["Token ung\u00FCltig \u2013 bitte erneut anmelden."]);
    }
  }
  renderUser();
  applyRoleNav();
}

// Ask the API which login UI to present (open/token/password). In password mode
// the SPA gates the whole app behind a login screen; the manual token field is
// hidden because the server issues session tokens via /auth/login.
async function loadAuthConfig() {
  try {
    const cfg = await api.get("/auth/config");
    state.authMode = cfg.mode || "open";
    state.passwordLogin = !!cfg.password_login;
    // Demo-only conveniences (absent/false outside PROCWORKS_DEMO_MODE).
    state.demo = !!cfg.demo;
    state.demoPassword = cfg.demo_password || "";
    state.demoAutologin = cfg.demo_autologin || "";
    state.demoLogins = Array.isArray(cfg.demo_logins) ? cfg.demo_logins : [];
    state.demoFeedbackUrl = cfg.demo_feedback_url || "";
    // OIDC-Redirect-Login (JWT-Modus, Opt-in per Server-Konfiguration):
    // nur wenn alle drei Angaben da sind, bietet die SPA den Firmen-Login an.
    state.oidc = (cfg.mode === "jwt" && cfg.oidc_authorize_url && cfg.oidc_token_url && cfg.oidc_client_id)
      ? { authorizeUrl: cfg.oidc_authorize_url, tokenUrl: cfg.oidc_token_url,
          clientId: cfg.oidc_client_id, scopes: cfg.oidc_scopes || "openid profile email" }
      : null;
  } catch (_e) {
    state.authMode = "open";
    state.passwordLogin = false;
    state.demo = false;
    state.demoPassword = "";
    state.demoAutologin = "";
    state.demoLogins = [];
    state.demoFeedbackUrl = "";
    state.oidc = null;
  }
  const tokenField = byId("token-field");
  if (tokenField) tokenField.style.display = state.passwordLogin ? "none" : "";
}

// --- OIDC-Redirect-Login (Opt-in) ------------------------------------------
// Authorization Code + PKCE, komplett im Client: der Kern rendert weiterhin
// keinen IdP-spezifischen Fluss, er reicht nur die konfigurierten Endpunkte
// über /auth/config durch. Ohne Konfiguration bleibt das Token-Feld der
// dokumentierte Weg. PKCE (S256) statt Client-Secret: die SPA ist ein
// öffentlicher Client; Verifier und State leben nur im sessionStorage des
// laufenden Anmeldevorgangs.

function _oidcRandom() {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return _b64url(bytes);
}

function _b64url(bytes) {
  return btoa(String.fromCharCode(...bytes))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Die eigene Rücksprung-Adresse: Origin + Pfad ohne Query/Fragment. Muss beim
// IdP als Redirect-URI des Clients registriert sein.
function _oidcRedirectUri() {
  return location.origin + location.pathname;
}

async function startOidcLogin() {
  if (!state.oidc) return;
  const verifier = _oidcRandom();
  const stateVal = _oidcRandom();
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const challenge = _b64url(new Uint8Array(digest));
  sessionStorage.setItem("oidcVerifier", verifier);
  sessionStorage.setItem("oidcState", stateVal);
  const q = new URLSearchParams({
    response_type: "code",
    client_id: state.oidc.clientId,
    redirect_uri: _oidcRedirectUri(),
    scope: state.oidc.scopes,
    state: stateVal,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  location.assign(state.oidc.authorizeUrl + (state.oidc.authorizeUrl.includes("?") ? "&" : "?") + q.toString());
}

// Rücksprung vom IdP: ?code=…&state=… gegen den Verifier einlösen. Liefert
// true, wenn ein Token übernommen wurde; räumt Query-Parameter und Merker in
// jedem Fall (auch ein abgebrochener Versuch hinterlässt keinen Zustand).
async function completeOidcLogin() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  const returnedState = params.get("state");
  if (!code || !state.oidc) return false;
  const verifier = sessionStorage.getItem("oidcVerifier");
  const expected = sessionStorage.getItem("oidcState");
  sessionStorage.removeItem("oidcVerifier");
  sessionStorage.removeItem("oidcState");
  history.replaceState(null, "", _oidcRedirectUri());
  if (!verifier || !expected || returnedState !== expected) {
    toast("err", "Anmeldung abgebrochen", ["Der Rücksprung passt nicht zum gestarteten Anmeldevorgang (State-Prüfung)."]);
    return false;
  }
  try {
    const res = await fetch(state.oidc.tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        code,
        redirect_uri: _oidcRedirectUri(),
        client_id: state.oidc.clientId,
        code_verifier: verifier,
      }).toString(),
    });
    if (!res.ok) throw new Error("token endpoint " + res.status);
    const body = await res.json();
    if (!body.access_token) throw new Error("kein access_token in der Antwort");
    state.token = String(body.access_token);
    localStorage.setItem("authToken", state.token);
    return true;
  } catch (err) {
    toast("err", "Anmeldung fehlgeschlagen", ["Der Identitätsanbieter hat den Code nicht eingelöst.", String(err && err.message || err)]);
    return false;
  }
}

// Vollbild-Anmeldekarte des JWT-Modus mit konfiguriertem Firmen-Login. Der
// manuelle Weg (Token ins Seitenleisten-Feld) bleibt als Ausweich erhalten.
function showOidcLoginOverlay() {
  const card = el("div", { class: "auth-card" },
    authBrand("Anmeldung über Ihren Firmen-Identitätsanbieter."),
    el("button", { class: "btn primary", onClick: () => startOidcLogin() },
      "Über Firmenkonto anmelden"),
    el("p", { class: "auth-hint", style: "margin-top:10px" },
      "Alternativ können Sie ein Zugangs-Token direkt in das Feld „API-Token“ der Seitenleiste eintragen. ",
      el("a", { href: "#", onClick: (e) => { e.preventDefault(); hideOverlay(); } }, "Ohne Anmeldung fortfahren")));
  showOverlay(card);
}

function showOverlay(card) {
  const root = byId("auth-overlay");
  clear(root);
  root.appendChild(card);
  root.style.display = "grid";
}

function hideOverlay() {
  const root = byId("auth-overlay");
  clear(root);
  root.style.display = "none";
}

function authBrand(subtitle) {
  return el("div", { class: "auth-brand" },
    el("div", { class: "logo" }, "CbC"),
    el("div", {},
      el("h2", {}, "ProcWorks"),
      el("div", { class: "auth-hint" }, subtitle)));
}

// Full-screen login: exchange username + password for a session token, store it
// and continue booting. A forced password change is handled right after.
function showLoginOverlay() {
  const errBox = el("div", { class: "auth-err" });
  const loginInput = el("input", { type: "text", id: "login-name", autocomplete: "username", placeholder: "vorname.nachname" });
  const pwInput = el("input", { type: "password", id: "login-pw", autocomplete: "current-password", placeholder: "Passwort" });
  const submit = async (e) => {
    if (e) e.preventDefault();
    errBox.textContent = "";
    try {
      const res = await api.post("/auth/login", {
        login: loginInput.value.trim(),
        password: pwInput.value,
      });
      state.token = res.token;
      localStorage.setItem("authToken", state.token);
      if (res.must_change) {
        showChangePasswordOverlay(true);
      } else {
        hideOverlay();
        await boot();
      }
    } catch (err) {
      // 429: zu viele Fehlversuche (Drosselung im Kern) -- der Kern
      // nennt die Wartezeit, der Text wird unveraendert gezeigt.
      errBox.textContent = err && err.status === 401
        ? "Login oder Passwort ist falsch."
        : err && err.status === 429 && typeof err.detail === "string"
          ? err.detail
          : "Anmeldung fehlgeschlagen.";
    }
  };
  const form = el("form", { onSubmit: submit },
    el("label", { class: "field" }, "Login", loginInput),
    el("label", { class: "field" }, "Passwort", pwInput),
    errBox,
    el("button", { class: "btn primary", type: "submit" }, "Anmelden"));
  const card = el("div", { class: "auth-card" },
    authBrand("Bitte melden Sie sich an."), form,
    el("p", { class: "auth-disclaimer" },
      "Nutzung auf eigenes Risiko. ProcWorks wird ohne jede Gewährleistung und ",
      "ohne jede Haftung bereitgestellt – für keinerlei Schäden an Systemen, ",
      "Daten oder Prozessen. ",
      el("a", {
        href: DISCLAIMER_URL,
        target: "_blank", rel: "noopener",
      }, "Haftungsausschluss")));
  showOverlay(card);
  setTimeout(() => loginInput.focus(), 0);
}

// Forced (first login) or self-service password change. On success we have a
// usable session and boot the app.
function showChangePasswordOverlay(forced) {
  const errBox = el("div", { class: "auth-err" });
  const curInput = el("input", { type: "password", autocomplete: "current-password", placeholder: "Aktuelles Passwort" });
  const newInput = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort (min. 8 Zeichen)" });
  const repInput = el("input", { type: "password", autocomplete: "new-password", placeholder: "Neues Passwort wiederholen" });
  const submit = async (e) => {
    if (e) e.preventDefault();
    errBox.textContent = "";
    if (newInput.value !== repInput.value) {
      errBox.textContent = "Die Passw\u00F6rter stimmen nicht \u00FCberein.";
      return;
    }
    try {
      await api.post("/auth/change-password", {
        current_password: curInput.value,
        new_password: newInput.value,
      });
      hideOverlay();
      toast("ok", "Passwort ge\u00E4ndert", ["Sie sind jetzt angemeldet."]);
      await boot();
    } catch (err) {
      // Der Kern nennt den konkreten Grund (``PW.too-short``/``PW.unchanged``);
      // formuliert wird im Meldungskatalog wie jede andere Absage.
      errBox.textContent = err && err.status === 400
        ? describeError(err).title
        : (err && err.status === 401
          ? "Aktuelles Passwort ist falsch."
          : "\u00C4nderung fehlgeschlagen.");
    }
  };
  const subtitle = forced
    ? "Bitte vergeben Sie ein eigenes Passwort."
    : "Passwort \u00E4ndern.";
  const form = el("form", { onSubmit: submit },
    el("label", { class: "field" }, "Aktuelles Passwort", curInput),
    el("label", { class: "field" }, "Neues Passwort", newInput),
    el("label", { class: "field" }, "Wiederholen", repInput),
    errBox,
    el("button", { class: "btn primary", type: "submit" }, "Speichern"));
  const card = el("div", { class: "auth-card" }, authBrand(subtitle), form);
  showOverlay(card);
  setTimeout(() => curInput.focus(), 0);
}

/**
 * localStorage-Schluessel: Login (``principal.subject``), dem die gemerkten
 * Sitzungsdaten gehoeren (Pruefinstanz-Auswahl u. ae.). Meldet sich jemand
 * anderes an, verwirft ``loadPrincipal`` diese Daten, statt sie zu uebernehmen.
 */
const SESSION_OWNER_KEY = "sessionOwner";

/**
 * localStorage-Schluessel, die an der angemeldeten Person haengen und beim
 * Sitzungswechsel verworfen werden (``resetSessionState``): ``agentId`` ist
 * die in „Meine Aufgaben“ gewaehlte Person – sonst oeffnete die naechste
 * Anmeldung ohne eigene Bearbeiter-Zuordnung direkt deren Liste. Wer einen
 * neuen personenbezogenen Schluessel einfuehrt, traegt ihn hier ein.
 */
const SESSION_STORAGE_KEYS = ["agentId"];

/**
 * Verwirft alles, was an der Anmeldung der vorigen Person haengt.
 *
 * Zweck: Nach einem Abmelden oder Login-Wechsel im selben Browser darf die
 * naechste Person in keiner Sicht Daten der vorigen sehen. Der zuletzt
 * geladene Vorgang stand sonst samt Instanzdaten weiter in der Ausfuehrung,
 * obwohl der Kern ihn der neuen Person verweigert (er liest einzelne Vorgaenge
 * nur fuer Beteiligte).
 *
 * Geleert werden: der geladene Vorgang und seine Arbeitsliste (auch der fuer
 * ein Neuladen gemerkte Vorgang, ``forgetInstance``), die
 * Pruefinstanz-Auswahl (auch ihr localStorage-Spiegel), das
 * Personenverzeichnis, Schema-Liste/-Namen und das geladene Schema samt
 * Validierung und Hinweisen (``boot`` laedt sie mit den Rechten der neuen
 * Person neu), Verbindungsstatus der Konnektoren, Fokus-/Ruecksprung-Marken,
 * die Basis fuer „neue Aufgabe eingetroffen“, offene Dialoge und der
 * gezeichnete Seiteninhalt.
 *
 * Bewusst erhalten: gewaehlte Sicht und gewaehlter Prozess (``view``,
 * ``schemaId``) – sie tragen selbst keine Daten; der Prozess wird beim
 * naechsten ``boot`` mit den Rechten der neuen Person nachgeladen, und eine
 * fuer sie gesperrte Sicht faengt ``applyRoleNav`` ab. Ebenso reine
 * Darstellungswahl (Farbschema, Menue, Modellier-Oberflaeche).
 *
 * Keine Parameter, kein Rueckgabewert; ohne DOM (Tests) laeuft es ebenfalls.
 */
function resetSessionState() {
  state.instanceIds = [];
  state.instanceId = null;
  state.instance = null;
  state.worklist = null;
  state.testInstanceId = null;
  state.testInstance = null;
  state.testStarter = null;
  state.testAgentA = null;
  state.testAgentB = null;
  persistTestState();
  state.simulation = null;
  state.agentDirectory = {};
  state.schemaIds = [];
  state.schemaNames = {};
  state.schemaVersions = {};
  state.schema = null;
  state.validation = null;
  state.hints = [];
  state.migrationReport = null;
  state.connectorStatus = {};
  state.revision = 0;
  state.selectedNode = null;
  state.dataFocusNode = null;
  state.staffFocusNode = null;
  state.returnTo = null;
  state.dataElemFocus = null;
  state.orgFocusUnit = null;
  state.orgFocusAgents = [];
  taskAlert = { agentId: null, keys: new Set(), primed: false };
  forgetInstance();
  // Gemerkte Werte, die an der Person haengen (die Pruefinstanz-Schluessel
  // raeumt persistTestState oben ab). Reine Darstellungswahl bleibt.
  SESSION_STORAGE_KEYS.forEach((k) => {
    try { localStorage.removeItem(k); } catch (_e) { /* nur Komfort */ }
  });
  const content = byId("content");
  if (content) clear(content);
  const modals = byId("modal-root");
  if (modals) clear(modals);
}

/**
 * Nimmt den angezeigten Vorgang aus der Ansicht, wenn der Kern ihn nicht
 * (mehr) herausgibt.
 *
 * Ein 404 bzw. 403 beim Nachladen heisst: Der Vorgang ist geloescht, oder die
 * aktuelle Anmeldung darf ihn nicht lesen. Dann darf der zuvor geladene Stand
 * nicht weiter angezeigt werden – sonst stuende ein fremder Vorgang samt Daten
 * auf dem Schirm, obwohl der Kern ihn verweigert.
 *
 * @param {*} err Fehlerobjekt aus ``request`` (``{status, detail}``) oder etwas anderes.
 * @returns {boolean} true, wenn der Vorgang verworfen und ein Hinweis gezeigt
 *   wurde; false bei jedem anderen Fehler (z. B. Verbindungsabbruch) – dann
 *   bleibt der Stand stehen, weil der Vorgang vermutlich noch existiert.
 */
function dropUnreadableInstance(err) {
  if (!err || (err.status !== 403 && err.status !== 404)) return false;
  forgetInstance();
  state.instanceId = null;
  state.instance = null;
  state.worklist = null;
  toast("info", "Vorgang nicht mehr verfügbar",
    ["Er wurde gelöscht, oder diese Anmeldung darf ihn nicht einsehen."]);
  return true;
}

// End the session server-side, drop the local token and return to the login.
// Alles Sitzungsbezogene wird verworfen (resetSessionState) – die naechste
// Person am selben Browser sieht nichts von dieser Sitzung.
async function logout() {
  try {
    await api.post("/auth/logout");
  } catch (_e) {
    // ignore: the token is dropped locally regardless.
  }
  state.token = "";
  state.principal = null;
  clearToasts();  // nichts von dieser Sitzung bleibt fuer die naechste stehen
  resetSessionState();
  localStorage.removeItem("authToken");
  try { localStorage.removeItem(SESSION_OWNER_KEY); } catch (_e) { /* nur Komfort */ }
  if (state.passwordLogin) showLoginOverlay();
  else await boot();
}

// --- Public-demo login conveniences (demo mode only) ----------------------

// Log in as a seeded demo user by exchanging the advertised demo credentials
// for a session token (via the normal /auth/login path -- no auth bypass).
// Returns true on success. Used for the silent auto-login and role switching.
// Demo users skip the forced first-change, so no password-change step follows.
/** sessionStorage: zuletzt benutzte Demo-Person (fuer die Wiederanmeldung). */
const DEMO_LAST_LOGIN_KEY = "demoLastLogin";

async function demoLoginAs(login) {
  try {
    const res = await api.post("/auth/login", { login, password: state.demoPassword });
    storageSet(sessionStorage, DEMO_LAST_LOGIN_KEY, login);
    state.token = res.token;
    localStorage.setItem("authToken", state.token);
    return true;
  } catch (_e) {
    return false;
  }
}

// One-click role switch from the demo banner: re-authenticate as another seeded
// demo user and reboot the app so nav/permissions reflect the new role.
async function switchDemoRole(login) {
  if (login === (state.principal && state.principal.subject)) return;
  const ok = await demoLoginAs(login);
  if (ok) {
    // Wer die Leiste geschlossen hatte, sieht sie nach dem Wechsel nur
    // eingeklappt wieder -- als Hinweis auf die neue Rolle, nicht als volle
    // Leiste, die sich jedes Mal neu aufdraengt.
    if (sessionStorage.getItem("demoBannerDismissed") === "1") {
      storageSet(localStorage, DEMO_BANNER_COLLAPSED_KEY, "1");
    }
    sessionStorage.removeItem("demoBannerDismissed");
    hideOverlay();
    await boot();
  } else {
    toast("err", "Rollenwechsel fehlgeschlagen", ["Bitte erneut versuchen."]);
  }
}

// Mount (or refresh) the dismissible demo banner: shows the current demo
// identity and offers one-click switches to the other seeded roles plus the
// shared password. Idempotent -- re-created on each boot so it reflects the
// active role; stays hidden once dismissed for the session.
// Derive the demo's own trial id from its hostname (trial-<id>.fly.dev). Sent
// with the survey so the operator can match feedback to the earlier lead mail
// from the same session (both carry the id). Empty when not on a trial host.
function currentTrialId() {
  const m = /^trial-([0-9a-f]+)\./.exec(window.location.hostname);
  return m ? m[1] : "";
}

// A 1..5 radio scale as one row of options; read the pick with
// ``node.querySelector('input:checked')`` after submit.
function scaleField(name) {
  return el("div", { class: "survey-scale" },
    ...[1, 2, 3, 4, 5].map((i) =>
      el("label", { class: "survey-scale-opt" },
        el("input", { type: "radio", name, value: String(i) }),
        el("span", {}, String(i)))));
}

// The post-demo, 2-minute survey ("Demo beenden"). Purely a demo feature: it is
// only reachable when the server advertised a broker feedback URL. The answers
// are POSTed cross-origin to the broker (which relays them by e-mail and stores
// nothing); delivery is best-effort, so the visitor is always thanked, even if
// the POST fails. Nothing here touches the correctness core.
// sessionStorage key: set as soon as the survey has been shown once (by the
// "Demo beenden" button or by the exit-intent hook below), so a visitor is
// asked at most once per demo session and never nagged.
const SURVEY_OFFERED_KEY = "demoSurveyOffered";

function endDemoSurvey() {
  if (!state.demoFeedbackUrl) return;
  sessionStorage.setItem(SURVEY_OFFERED_KEY, "1");
  const roleSel = el("select", { class: "input" },
    el("option", { value: "" }, "– bitte wählen –"),
    ...["Fachbereich / Prozessverantwortung", "IT / Entwicklung", "Beratung / Consulting",
      "Management / Geschäftsführung", "Lehre / Studium", "Sonstiges"]
      .map((r) => el("option", { value: r }, r)));
  const intentSel = el("select", { class: "input" },
    el("option", { value: "" }, "– bitte wählen –"),
    ...["Ja, konkret", "Ja, perspektivisch", "Eher nicht", "Nein"]
      .map((v) => el("option", { value: v }, v)));
  const comment = el("textarea", { class: "input", rows: "3", maxlength: "2000",
    placeholder: "Was hat gefehlt, was war unklar, was würdest du verbessern?" });

  const field = (label, node) => el("label", { class: "survey-field" },
    el("span", { class: "survey-q" }, label), node);
  const body = el("div", { class: "survey" },
    field("Was beschreibt dich am besten?", roleSel),
    field("Wie zufrieden bist du mit ProcWorks insgesamt? (1 = gar nicht, 5 = sehr)",
      scaleField("satisfaction")),
    field("ProcWorks lässt per Konstruktion keine fehlerhaften Prozessmodelle zu. "
      + "Wie wichtig ist dir das? (1–5)", scaleField("cbc")),
    field("Wie leicht fiel dir das Modellieren? (1–5)", scaleField("ease")),
    field("Könntest du dir vorstellen, ProcWorks einzusetzen?", intentSel),
    field("Was würdest du verbessern? (optional)", comment));

  const scale = (name) => {
    const hit = body.querySelector(`input[name="${name}"]:checked`);
    return hit ? Number(hit.value) : null;
  };
  openModal("Demo beenden – 2-Minuten-Feedback", body, async () => {
    const payload = {
      trial_id: currentTrialId(),
      role: roleSel.value,
      satisfaction: scale("satisfaction"),
      cbc_importance: scale("cbc"),
      ease: scale("ease"),
      intent: intentSel.value,
      comment: comment.value,
    };
    try {
      await fetch(state.demoFeedbackUrl, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
    } catch (_e) { /* best-effort: thank the visitor regardless */ }
    toast("success", "Danke für dein Feedback!",
      ["Es hilft uns, ProcWorks zu verbessern. Du kannst den Tab jetzt schließen."]);
    return true;
  }, "Absenden & beenden");
}

// Minimum time on the page before the exit-intent survey may appear. A visitor
// who tabs away in the first minute has not seen enough to answer, and being
// asked immediately reads as nagging.
const SURVEY_MIN_DWELL_MS = 60000;

// Offer the survey when the visitor *leaves* the tab, not only when they use
// the "Demo beenden" button -- closing the window is otherwise the one exit
// that yields no feedback at all.
//
// Deliberately hooked to ``visibilitychange`` rather than ``beforeunload``:
// browsers forbid arbitrary UI (and any reliable async work) during unload, so
// a survey shown there would either be suppressed or lose the answers. Here the
// modal is opened while the tab is hidden and is simply *waiting* when the
// visitor comes back. Someone who never returns is unreachable either way --
// that case is a mail, which we deliberately do not send (contact data lives
// only for the duration of the lead request, never stored).
//
// Guarded so it fires at most once, only in the public demo (a feedback URL is
// configured), only after a real dwell time, and never on top of an open modal
// or a half-filled form (``userIsBusy``).
function installExitIntentSurvey() {
  if (!state.demoFeedbackUrl) return;
  if (sessionStorage.getItem(SURVEY_OFFERED_KEY) === "1") return;
  const armedAt = Date.now();
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") return;
    if (sessionStorage.getItem(SURVEY_OFFERED_KEY) === "1") return;
    if (Date.now() - armedAt < SURVEY_MIN_DWELL_MS) return;
    if (userIsBusy()) return;
    if (typeof Tour !== "undefined" && Tour.running) return;   // Tour laeuft
    endDemoSurvey();   // marks the session as offered
  });
}

/** localStorage-Schluessel: Demo-Banner eingeklappt (ueberlebt Neuladen). */
const DEMO_BANNER_COLLAPSED_KEY = "demoBannerCollapsed";

/** Liest einen Speicherwert fehlertolerant (privater Modus -> null). */
function storageGet(store, key) {
  try { return store.getItem(key); } catch (e) { return null; }
}
/** Schreibt einen Speicherwert fehlertolerant (privater Modus -> ignoriert). */
function storageSet(store, key, value) {
  try { store.setItem(key, value); } catch (e) { /* nur Komfort */ }
}

/**
 * Reserviert unten Platz fuer den Demo-Banner, statt ihn ueber die App zu legen.
 *
 * Der Banner ist fest positioniert. Frueher lag er ueber allem -- auch ueber dem
 * unteren Teil der Schritt-Karte, ueber Dialogen und Toasts. Jetzt meldet er seine Hoehe als CSS-Variable `--demo-banner-h`;
 * `.app` wird um genau diese Hoehe kuerzer und die Toasts sitzen darueber.
 * Ein ResizeObserver haelt den Wert beim Umbrechen (schmales Fenster) aktuell.
 * @param {HTMLElement|null} banner der Banner oder null (entfernt)
 */
function reserveDemoBannerSpace(banner) {
  const root = document.documentElement;
  if (reserveDemoBannerSpace._obs) { reserveDemoBannerSpace._obs.disconnect(); reserveDemoBannerSpace._obs = null; }
  const apply = () => {
    const h = banner && banner.isConnected ? Math.ceil(banner.getBoundingClientRect().height) + 20 : 0;
    root.style.setProperty("--demo-banner-h", h + "px");
  };
  apply();
  if (banner && typeof ResizeObserver !== "undefined") {
    reserveDemoBannerSpace._obs = new ResizeObserver(apply);
    reserveDemoBannerSpace._obs.observe(banner);
  }
}

function mountDemoBanner() {
  const old = byId("demo-banner");
  if (old) old.remove();
  if (sessionStorage.getItem("demoBannerDismissed") === "1") { reserveDemoBannerSpace(null); return; }

  // Eingeklappt: nur ein kleiner Knopf, der den Banner wieder oeffnet.
  if (storageGet(localStorage, DEMO_BANNER_COLLAPSED_KEY) === "1") {
    const pill = el("button", {
      id: "demo-banner", class: "demo-banner demo-banner-collapsed",
      title: "Demo-Hinweis mit Rollenwechsel und Passwort öffnen",
      "aria-label": "Demo-Hinweis öffnen",
      onClick: () => { storageSet(localStorage, DEMO_BANNER_COLLAPSED_KEY, "0"); mountDemoBanner(); },
    }, el("span", { class: "demo-badge" }, "DEMO"), " Rollen & Passwort \u25B4");
    document.body.appendChild(pill);
    reserveDemoBannerSpace(pill);
    return;
  }

  const meLogin = state.principal && state.principal.subject;
  const meName = (state.principal && state.principal.display_name) || meLogin || "–";
  const meRole = ((state.principal && state.principal.roles) || [])
    .map((r) => ROLE_LABELS[r] || r).join(", ") || "ohne Rolle";

  const others = state.demoLogins.filter((u) => u.login !== meLogin);
  const caption = (u) => `${ROLE_LABELS[u.role] || u.role}: ${u.name}`;
  // Auf dem Handy eine Auswahlliste statt eines Knopfs je Rolle: Die
  // 13 Knoepfe brachen auf 390 px in viele Zeilen um, die Leiste wurde so hoch,
  // dass die App (sie macht der Leiste Platz, --demo-banner-h) kaum noch Raum
  // hatte -- und die Tour ihre Blase oben ueber den Menueknopf legen musste.
  const narrow = typeof window !== "undefined" && window.innerWidth <= 720;
  const switches = narrow && others.length
    ? [el("select", {
        class: "demo-switch-select", "aria-label": "Andere Rolle testen",
        onChange: (e) => { if (e.target.value) switchDemoRole(e.target.value); },
      },
        el("option", { value: "" }, "Andere Rolle testen \u2026"),
        ...others.map((u) => el("option", { value: u.login }, caption(u))))]
    : others.map((u) => el("button", {
        class: "demo-switch",
        title: `Als ${u.name} anmelden`,
        onClick: () => switchDemoRole(u.login),
      }, caption(u)));

  const banner = el("div", { id: "demo-banner", class: "demo-banner", role: "region", "aria-label": "Demo-Hinweis" },
    el("div", { class: "demo-banner-main" },
      el("span", { class: "demo-badge" }, "DEMO"),
      el("span", {}, `Angemeldet als ${meName} (${meRole}).`),
      switches.length
        ? el("span", { class: "demo-switch-wrap" },
            narrow ? null : el("span", { class: "demo-switch-label" }, "Andere Rolle testen:"),
            ...switches)
        : null,
      state.demoPassword
        ? el("span", { class: "demo-pw" }, `Passwort: ${state.demoPassword}`)
        : null),
    el("div", { class: "demo-banner-actions" },
      state.demoFeedbackUrl
        ? el("button", {
            class: "demo-end", title: "Demo beenden und kurz Feedback geben",
            onClick: endDemoSurvey,
          }, "Demo beenden")
        : null,
      el("button", {
        class: "demo-banner-close", "aria-label": "Hinweis einklappen", title: "Einklappen",
        onClick: () => { storageSet(localStorage, DEMO_BANNER_COLLAPSED_KEY, "1"); mountDemoBanner(); },
      }, "\u25BE"),
      el("button", {
        class: "demo-banner-close", "aria-label": "Hinweis schließen", title: "Für diese Sitzung schließen",
        onClick: () => { sessionStorage.setItem("demoBannerDismissed", "1"); banner.remove(); reserveDemoBannerSpace(null); },
      }, "×")));

  document.body.appendChild(banner);
  reserveDemoBannerSpace(banner);
}


function renderUser() {
  const pill = byId("user-pill");
  const foot = byId("auth-user");
  const logoutBtn = byId("logout-btn");
  const p = state.principal;
  const bound = p && p.agent_id;
  const roles = (p && p.roles) || [];
  const roleText = roles.map((r) => ROLE_LABELS[r] || r).join(", ");
  const showLogout = state.passwordLogin && !!p;
  if (logoutBtn) logoutBtn.style.display = showLogout ? "" : "none";
  if (!p) {
    pill.textContent = "nicht angemeldet";
    pill.className = "pill pill-gray";
    foot.textContent = "Nicht angemeldet";
    return;
  }
  // Open dev mode: anonymous principal with all roles -> show "offen".
  const open = !bound && roles.length >= 4;
  pill.textContent = open ? "offen" : (p.display_name || p.subject);
  pill.className = "pill " + (open ? "pill-gray" : "pill-green");
  if (showLogout) {
    clear(foot);
    foot.appendChild(el("span", {}, `${p.display_name || p.subject} \u00B7 ${roleText || "ohne Rolle"}`));
    foot.appendChild(document.createTextNode(" \u00B7 "));
    foot.appendChild(el("a", {
      href: "#", onClick: (e) => { e.preventDefault(); showChangePasswordOverlay(false); },
    }, "Passwort \u00E4ndern"));
    return;
  }
  foot.textContent = open
    ? "Modus: offen (kein Login)"
    : `${p.display_name || p.subject} \u00B7 ${roleText || "ohne Rolle"}`;
}

// Hide nav entries the current role may not use and keep the active view valid.
function applyRoleNav() {
  const buttons = [...byId("nav").children];
  buttons.forEach((b) => {
    const allowed = VIEW_ROLES[b.dataset.view] || [];
    const visible = hasRole(...allowed);
    b.style.display = visible ? "" : "none";
  });
  const allowedNow = hasRole(...(VIEW_ROLES[state.view] || []));
  if (!allowedNow) {
    const first = buttons.find((b) => b.style.display !== "none");
    if (first) state.view = first.dataset.view;
  }
}

async function setToken(token) {
  state.token = token.trim();
  if (state.token) localStorage.setItem("authToken", state.token);
  else localStorage.removeItem("authToken");
  await boot();
}

// Laeuft gerade eine Sichtfunktion? Und wurde waehrenddessen erneut gerendert?
// Siehe render() -- diese beiden Merker verhindern das Ueberlappen zweier
// Renderlaeufe.
let renderBusy = false;
let renderQueued = false;
// Nur wenn ALLE vorgemerkten Laeufe die Rollposition behalten wollen
// (Live-Aktualisierung), behaelt sie auch der nachgeholte Lauf.
let renderQueuedKeepScroll = true;

/**
 * Hat die Person im Inhaltsbereich etwas eingegeben, das noch nicht
 * abgeschickt ist? Gesetzt von ``markContentDirty`` (input/change an einem
 * Formularfeld in ``#content``), geloescht bei jedem Neuzeichnen der Sicht –
 * danach steht ohnehin ein frisches Formular da. Solange es gesetzt ist,
 * zeichnet die automatische Aktualisierung nicht neu (``userIsBusy``); sie
 * bietet stattdessen „Aktualisieren“ an (``showLiveRefreshHint``).
 */
let contentDirty = false;

/**
 * Zeichnet die aktuelle Sicht neu.
 *
 * @param {{keepScroll?: boolean}} [opts] ``keepScroll: true`` (automatische
 *   Aktualisierung): Rollposition des Inhalts danach wiederherstellen.
 *   Ohne Option (Aktion der Person, Sichtwechsel) beginnt die Sicht oben.
 *
 * **Renderlaeufe ueberlappen sich nie.** Die Sichtfunktionen sind asynchron und
 * folgen alle demselben Muster: erst ``clear(content)``, dann ``await api.get``,
 * dann anhaengen. Starten zwei Laeufe kurz nacheinander, leert der zweite den
 * Inhalt, waehrend der erste noch auf die API wartet -- und danach haengen
 * *beide* ihre Panels an. Sichtbar wurde das als **doppelte Bereiche** in „Meine
 * Aufgaben" (zweimal „Offene Aufgaben", zweimal „Abwesenheit").
 *
 * Ausloeser gibt es auf den Laufzeit-Sichten reichlich, und sie treffen sich
 * genau beim Erledigen einer Aufgabe: der Klick-Callback, der Revisions-Poll
 * (das Erledigen erhoeht die Revision), der Zeit-Tick und -- waehrend des
 * Tutorials -- der Tour-Tick, der den Schritt weiterschaltet. Deshalb fiel es im
 * Tutorial auf; der Fehler ist aber nicht tutorial-spezifisch.
 *
 * Statt jede Sicht einzeln abzusichern, werden die Laeufe hier **zusammen-
 * gefasst**: waehrend einer laeuft, wird ein weiterer nur vorgemerkt und danach
 * genau einmal nachgeholt. Der zuletzt gewuenschte Zustand wird also immer
 * gezeichnet, nur eben nacheinander statt verschraenkt.
 */
function render(opts) {
  const keepScroll = !!(opts && opts.keepScroll);
  if (renderBusy) {
    renderQueued = true;
    renderQueuedKeepScroll = renderQueuedKeepScroll && keepScroll;
    return;
  }
  renderBusy = true;
  // Neu gezeichnet = frisches Formular: nichts Ungespeichertes mehr auf dem Schirm.
  contentDirty = false;
  // Live-Aktualisierung: an derselben Stelle bleiben, statt nach oben zu springen.
  const scroller = keepScroll ? document.querySelector(".main") : null;
  const scrollTop = scroller ? scroller.scrollTop : 0;
  // Guard against a stale/unknown persisted view (e.g. after a rename) so the
  // dispatch below never dereferences an undefined entry.
  if (!VIEW_META[state.view]) state.view = "model";
  // Der Kontrollfluss-Vollbildmodus lebt nur in der Modellieren-Sicht; beim
  // Verlassen zuruecksetzen, damit er nicht als Overlay in anderen Sichten haengt.
  if (state.view !== "model") state.graphMaximized = false;
  // Remember the active view so a page reload restores it instead of always
  // falling back to "Modellieren".
  localStorage.setItem("view", state.view);
  const meta = VIEW_META[state.view];
  byId("view-title").textContent = meta.title;
  byId("view-sub").textContent = meta.sub;
  byId("view-sub").title = meta.sub;   // gekuerzt angezeigt, voll im Tooltip
  renderSchemaPicker();
  setActiveNav();
  renderTourBadge();
  Promise.resolve(meta.fn())
    .catch((err) => { toastError(err); })
    .then(() => { if (scroller) scroller.scrollTop = scrollTop; })
    // Die Sichten bauen ihr DOM bei jedem Rendern komplett neu auf -- der Anker
    // der laufenden Tour existiert danach nicht mehr und muss neu gesucht
    // werden. Gekapselt, damit ein Fehler in der Tour nie die Sicht mitreisst.
    .then(() => { if (typeof Tour !== "undefined") Tour.afterRender(); })
    // Erst hier ist die Sicht fertig aufgebaut. Ein waehrenddessen vorgemerkter
    // Lauf wird jetzt nachgeholt -- ``finally``, damit ein Fehler das Rendern
    // nicht dauerhaft blockiert (renderBusy bliebe sonst true).
    .catch(() => { /* Tour-Fehler: die Sicht steht, weiter geht es trotzdem */ })
    .finally(() => {
      renderBusy = false;
      if (renderQueued) {
        const keep = renderQueuedKeepScroll;
        renderQueued = false;
        renderQueuedKeepScroll = true;
        render({ keepScroll: keep });
      }
    });
}

/**
 * Zeigt in der Topbar das Abzeichen „Tutorial – es wird nichts gespeichert",
 * solange die Tour im schreibfreien Sandkasten laeuft.
 *
 * Ehrlichkeit gegenueber dem Nutzer: Er arbeitet dort auf einem Beispielprozess
 * und mit abgespielten Antworten -- das muss sichtbar sein, nicht versteckt.
 */
function renderTourBadge() {
  const slot = byId("tour-badge-slot");
  if (!slot) return;
  clear(slot);
  if (typeof Tour === "undefined" || !Tour.sandboxed) return;
  slot.appendChild(el("span", {
    class: "tour-badge",
    title: "Im Tutorial verl\u00E4sst kein schreibender Aufruf den Browser.",
  }, "Tutorial – es wird nichts gespeichert"));
}

// --------------------------------------------------------------------------
// Live-Aktualisierung (Auto-Refresh der Laufzeit-Sichten)
// --------------------------------------------------------------------------

// Views that mirror runtime progress and should refresh automatically when an
// activity/instance advances anywhere (e.g. another user completes a task).
// Modelling views are intentionally excluded so editing is never interrupted.
const LIVE_VIEWS = new Set(["run", "tasks", "testrun", "monitor"]);
const LIVE_POLL_MS = 4000;
let livePollBusy = false;

// True while the user is actively interacting (a modal/login overlay is open,
// the focus sits in a form field of the content area, or something was typed
// there and not yet sent -- contentDirty, regardless of focus). Auto-refresh is
// skipped then so it never wipes an open dropdown or a half-filled form.
function userIsBusy() {
  if (byId("modal-root").children.length) return true;
  const overlay = byId("auth-overlay");
  if (overlay && overlay.style.display !== "none") return true;
  if (contentDirty) return true;
  const active = document.activeElement;
  if (active && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName)) {
    const content = byId("content");
    if (content && content.contains(active)) return true;
  }
  return false;
}

/**
 * Merkt eine Eingabe im Inhaltsbereich als „noch nicht abgeschickt“.
 *
 * Haengt (in ``wireNav``) als input-/change-Lauscher am Dokument. Zaehlt nur
 * Formularfelder in ``#content``; Felder mit ``data-live-safe`` sind
 * ausgenommen – ihr Wert liegt in ``state`` und uebersteht ein Neuzeichnen
 * (Simulation). Ohne diese Marke verwarf die automatische Aktualisierung
 * halb ausgefuellte Formulare, sobald der Fokus das Feld verliess.
 *
 * @param {Event} e das Ereignis
 */
function markContentDirty(e) {
  const t = e && e.target;
  if (!t || !/^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName)) return;
  if (t.hasAttribute && t.hasAttribute("data-live-safe")) return;
  const content = byId("content");
  if (content && content.contains(t)) contentDirty = true;
}

/**
 * Zeigt oben in der Sicht „Neue Daten – Aktualisieren“, statt die Sicht mit
 * ungespeicherten Eingaben neu zu zeichnen. Einmal je Sicht (kein Stapeln).
 * Der Knopf zeichnet neu – bewusst durch die Person, denn dabei gehen die
 * Eingaben verloren (Tooltip sagt das).
 */
function showLiveRefreshHint() {
  const content = byId("content");
  if (!content || content.querySelector(".live-refresh-hint")) return;
  const hint = el("div", { class: "live-refresh-hint", role: "status" },
    el("span", null, "Neue Daten sind da."),
    el("button", { class: "btn small", type: "button",
      title: "Zeichnet die Ansicht neu \u2013 nicht abgeschickte Eingaben gehen dabei verloren.",
      onClick: async () => {
        if (state.view === "run" && state.instanceId) {
          try { await loadInstance(state.instanceId); } catch (e) { dropUnreadableInstance(e); }
        }
        render({ keepScroll: true });
      } }, "Aktualisieren"));
  content.insertBefore(hint, content.firstChild);
}

// Poll the cheap runtime-event revision; when it changed, refresh the current
// live view so task lists and monitoring follow progress without a manual
// reload. The "run" view caches the loaded instance, so reload it first.
async function pollLiveUpdates() {
  if (livePollBusy) return;
  if (state.passwordLogin && !state.principal) return;
  livePollBusy = true;
  try {
    const res = await api.get("/monitoring/revision");
    const rev = res && typeof res.revision === "number" ? res.revision : 0;
    if (rev === state.revision) return;
    state.revision = rev;
    if (!LIVE_VIEWS.has(state.view)) return;
    if (userIsBusy()) {
      // Ungespeicherte Eingaben (nicht Dialog/Anmeldung): Daten nicht still
      // verschlucken, sondern Neuladen anbieten.
      if (contentDirty && !byId("modal-root").children.length) showLiveRefreshHint();
      return;
    }
    if (state.view === "run" && state.instanceId) {
      // 404/403: geloescht oder nicht (mehr) lesbar -> aus der Ansicht nehmen,
      // nie den alten Stand stehen lassen. Andere Fehler (Netz) behalten ihn.
      try { await loadInstance(state.instanceId); } catch (e) { dropUnreadableInstance(e); }
    }
    render({ keepScroll: true });
  } catch (_e) {
    // Silent: a transient API hiccup must not spam toasts on a background poll.
  } finally {
    livePollBusy = false;
  }
}

// Views whose time-based worklist criticality "walks on" as wall-clock time
// passes, even without a new runtime event (Zeitbasierte-Priorisierung 7).
const TIME_TICK_VIEWS = new Set(["tasks", "testrun", "monitor", "run"]);
const TIME_TICK_MS = 30000;

// Re-render the current time-sensitive view on a slow cadence so the "Fällig"
// countdown and the criticality bands advance without a triggering event. Kept
// separate from the (event-driven) revision poll and skipped while the user is
// busy, so it never disturbs an open form or modal.
function tickTimeViews() {
  if (!TIME_TICK_VIEWS.has(state.view) || userIsBusy()) return;
  if (state.passwordLogin && !state.principal) return;
  render({ keepScroll: true });
}

// Start the background poll exactly once (boot may run repeatedly on re-login).
function startLiveUpdates() {
  if (startLiveUpdates._started) return;
  startLiveUpdates._started = true;
  setInterval(pollLiveUpdates, LIVE_POLL_MS);
  setInterval(tickTimeViews, TIME_TICK_MS);
}

/**
 * Haelt die App hinter einem Dialog oder der Anmeldemaske ``inert``.
 *
 * ``inert`` nimmt die Seite aus der Tab-Reihenfolge und fuer Klicks aus dem
 * Spiel. Ohne das sprang Tab aus der Anmeldemaske in die verdeckte App und
 * lief bei offenem Dialog durch die Liste dahinter. Ein Beobachter statt
 * Aufrufen in openModal/showOverlay: Dialoge werden an mehreren Stellen
 * geschlossen (Abbrechen, Hintergrund, Bestaetigen, Escape), und die Sperre
 * darf nie haengen bleiben.
 */
function watchInert() {
  const modalRoot = byId("modal-root");
  const overlay = byId("auth-overlay");
  if (!modalRoot || !overlay || typeof MutationObserver === "undefined") return;
  const obs = new MutationObserver(syncInert);
  obs.observe(modalRoot, { childList: true });
  obs.observe(overlay, { childList: true, attributes: true, attributeFilter: ["style"] });
  syncInert();
}

/** Setzt ``inert`` auf der App genau dann, wenn Dialog oder Anmeldemaske offen sind. */
function syncInert() {
  const app = document.querySelector(".app");
  const modalRoot = byId("modal-root");
  const overlay = byId("auth-overlay");
  if (!app || !modalRoot || !overlay) return;
  const blocked = modalRoot.childElementCount > 0 || overlay.style.display !== "none";
  if (blocked) app.setAttribute("inert", ""); else app.removeAttribute("inert");
}

function wireNav() {
  // Ungespeicherte Eingaben im Inhalt erkennen (schuetzt sie vor der
  // automatischen Aktualisierung, siehe markContentDirty).
  document.addEventListener("input", markContentDirty, true);
  document.addEventListener("change", markContentDirty, true);
  byId("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (!btn) return;
    state.view = btn.dataset.view;
    // A direct nav click is a fresh intent -- drop any badge-driven highlight
    // and the control-flow return point (it would otherwise be stale).
    state.dataFocusNode = null;
    state.staffFocusNode = null;
    state.orgFocusUnit = null;
    state.orgFocusAgents = [];
    state.returnTo = null;
    // Auf dem Smartphone faehrt die Menue-Schublade nach der Wahl wieder ein,
    // damit sofort der gewaehlte Inhalt (z. B. „Meine Aufgaben") sichtbar ist.
    closeMobileNav();
    render();
  });
  const sideToggle = byId("sidebar-toggle");
  if (sideToggle) sideToggle.addEventListener("click", toggleSidebar);
  // Mobile Menue-Schublade: Hamburger oeffnet/schliesst, Tipp auf den
  // abdunkelnden Hintergrund schliesst.
  const burger = byId("nav-burger");
  if (burger) burger.addEventListener("click", toggleMobileNav);
  const scrim = byId("nav-scrim");
  if (scrim) scrim.addEventListener("click", closeMobileNav);
  // Escape verlaesst das Kontrollfluss-Vollbild, hebt die Auswahl im
  // Kontrollfluss auf (beide Oberflaechen) bzw. schliesst die mobile Menue-Schublade (in dieser
  // Reihenfolge, jeweils nur wenn aktiv). Eingaben in einem Feld bleiben
  // unberuehrt -- sonst raeumte Escape mitten im Tippen die Karte weg.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (isTypingTarget()) return;
    // Steht ein Dialog offen, gehoert die Taste ihm -- die Sicht dahinter
    // duerfte sonst umbauen, waehrend der Dialog noch auf ihr arbeitet.
    const modal = byId("modal-root");
    if (modal && modal.childElementCount) return;
    if (state.graphMaximized) {
      state.graphMaximized = false;
      render();
    } else if (state.view === "model" && state.selectedNode) {
      state.selectedNode = null;
      render();
    } else if (document.documentElement.getAttribute("data-mobile-nav") === "open") {
      closeMobileNav();
    }
  });
  // Tastaturnavigation im Kontrollfluss (Stufe U4): Pfeiltasten
  // bewegen die Auswahl entlang des Spine bzw. zwischen den Zweigen, Enter holt
  // den Fokus in die Schritt-Karte. Gilt in beiden Modellier-Oberflaechen --
  // beide fuehren dieselbe Auswahl (state.selectedNode) und ruecken den
  // gewaehlten Knoten ins Bild.
  document.addEventListener("keydown", (e) => {
    const dir = ARROW_DIRS[e.key];
    if (!dir && e.key !== "Enter") return;
    // Mit Zusatztaste gehoert die Kombination dem Browser (Verlauf, Zeilenende,
    // Textauswahl) -- die wird nicht uebernommen.
    if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return;
    if (state.view !== "model") return;
    if (isTypingTarget()) return;
    const modal = byId("modal-root");
    if (modal && modal.childElementCount) return;   // Dialog hat Vorrang
    if (!dir) {
      // Enter nur aus dem Kontrollfluss heraus: liegt der Fokus schon auf einem
      // Bedienelement, ist Enter dessen Ausloeser und darf nicht abgefangen
      // werden.
      const tag = document.activeElement && document.activeElement.tagName;
      if (tag === "BUTTON" || tag === "A") return;
      if (focusStepCard()) e.preventDefault();
      return;
    }
    // Pfeiltasten scrollen sonst die Seite, waehrend die Auswahl wandert.
    if (moveSelection(dir)) e.preventDefault();
  });
  const apiInput = byId("api-base");
  apiInput.value = state.apiBase;
  apiInput.addEventListener("change", async () => {
    // Nur eine Adresse uebernehmen, hinter der wirklich ein ProcWorks-Kern
    // antwortet: Eine verirrte Eingabe („4“) legte sonst still jeden
    // Aufruf lahm und blieb im Browser gespeichert.
    const candidate = apiInput.value.trim() || defaultApiBase();
    if (!(await isProcWorksApi(candidate))) {
      toast("err", "Keine ProcWorks-API unter dieser Adresse",
        [`${candidate} antwortet nicht wie ein ProcWorks-Server. Die bisherige Adresse bleibt: ${state.apiBase}`]);
      apiInput.value = state.apiBase;
      return;
    }
    state.apiBase = candidate;
    localStorage.setItem("apiBase", state.apiBase);
    await boot();
  });
  const tokenInput = byId("auth-token");
  tokenInput.value = state.token;
  tokenInput.addEventListener("change", () => { setToken(tokenInput.value); });
  const logoutBtn = byId("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", () => { logout(); });
  wireTheme();
}

/**
 * Verdrahtet die Farbschema-Knoepfe (Dunkel/Hell) der Sidebar und markiert den
 * aktuell aktiven. Rein clientseitig -- kein API-Aufruf, kein boot().
 */
function wireTheme() {
  document.querySelectorAll(".theme-btn").forEach((btn) => {
    btn.addEventListener("click", () => setTheme(btn.dataset.themeChoice));
  });
  applyTheme(state.theme); // aktiven Knopf markieren, nun da er im DOM ist
}

async function boot() {
  try {
    const health = await api.get("/health");
    setConnected(true);
    showVersion(health && health.version);
    await loadAuthConfig();
    // OIDC-Rücksprung (JWT-Modus): einen mitgebrachten ?code= sofort gegen
    // ein Token einlösen, bevor irgendeine Gate-Entscheidung fällt.
    if (state.oidc) await completeOidcLogin();
    // JWT-Modus mit konfiguriertem Firmen-Login: ohne Token die Anmeldekarte
    // zeigen (der manuelle Token-Weg bleibt über die Seitenleiste offen).
    if (state.authMode === "jwt" && state.oidc && !state.token) {
      showOidcLoginOverlay();
      return;
    }
    // In password mode an unauthenticated visitor must log in first; the rest
    // of the app stays hidden behind the overlay until /auth/me succeeds.
    if (state.passwordLogin && !state.token) {
      // Public demo: silently auto-login as the modeler so the visitor lands
      // straight in the editor. Attempted once; on failure fall back to the
      // normal login overlay so the demo is never a dead end.
      let autologged = false;
      if (state.demo && state.demoAutologin && !state.demoAutologinTried) {
        state.demoAutologinTried = true;
        autologged = await demoLoginAs(state.demoAutologin);
      }
      if (!autologged) {
        showLoginOverlay();
        return;
      }
    }
    await loadPrincipal();
    if (state.passwordLogin && !state.principal) {
      // Demo mit abgelaufener Sitzung: Der 401 von /auth/me hat die
      // Wiederanmeldung schon angestossen (request); sie ruft boot() erneut.
      // Keine Anmeldemaske dazwischen.
      if (demoRecovering) return;
      showLoginOverlay();
      return;
    }
    hideOverlay();
    if (state.demo) mountDemoBanner();
    await loadSchemas();
    await refreshSchema();
    // Nach einem Neuladen beim zuletzt gewaehlten Vorgang bleiben (nur
    // derselbe Login, nur dieser Tab -- siehe rememberInstance).
    await restoreInstance();
    // Personenverzeichnis ueber alle Organisationen: Namen in Aufgaben-,
    // Audit- und Monitoring-Sichten sollen nicht davon abhaengen, welcher
    // Prozess oben gewaehlt ist ("Meine Aufgaben" zeigte sonst interne IDs).
    await loadAgentDirectory();
    // Baseline the live-update revision to "now" so the first poll only fires on
    // genuinely new progress, then start the background auto-refresh.
    try {
      const res = await api.get("/monitoring/revision");
      state.revision = res && typeof res.revision === "number" ? res.revision : 0;
    } catch (_e) { /* keep current baseline */ }
    startLiveUpdates();
    // Erstkontakt: die zur Rolle passende gefuehrte Tour anbieten, sofern sie
    // noch nicht erledigt oder dreimal verschoben wurde. In der oeffentlichen
    // Demo greift derselbe Pfad, weil sich der Besucher dort automatisch
    // anmeldet.
    if (typeof Tour !== "undefined") Tour.maybeOffer();
    // Nur in der oeffentlichen Demo wirksam: Umfrage auch beim Verlassen des
    // Tabs anbieten, nicht erst am "Demo beenden"-Knopf.
    installExitIntentSurvey();
  } catch (err) {
    setConnected(false);
    toastError(err);
  }
  render();
}

wireNav();
watchInert();
boot();
